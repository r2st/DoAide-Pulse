"""The beat tasks themselves — the wrappers, not the services under them.

``trigger_tasks`` had no coverage at all and ``maintenance_tasks`` only its
service layer, which left the parts that are specific to being a *task*
untested: opening and closing a session, the "never raises" contract that keeps
one bad trigger from taking a sweep down, the skip conditions that stop work on
a deactivated account, and the inline fallback when the broker is unreachable.
Those are exactly the paths that only ever run in production, and only ever run
when something else has already gone wrong.

Every task here is called directly rather than through Celery: ``.delay`` is
Celery's to test, and with ``CELERY_ENABLED=false`` the call is synchronous
anyway.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.models.mixins import utcnow
from app.models.password_reset import PasswordResetToken
from app.models.trigger import (
    Trigger,
    TriggerEvent,
    TriggerEventStatus,
    TriggerKind,
)
from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.services.password_reset import hash_token
from app.tasks import autopilot_tasks, maintenance_tasks, trigger_tasks


def _no_close(session):
    """The test session, wrapped so a task's ``db.close()`` does not end it."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    """Point both task modules at the test database.

    Each task opens its own ``SessionLocal``, which otherwise resolves to the
    real engine and finds no tables.
    """
    monkeypatch.setattr(trigger_tasks, "SessionLocal", _no_close(db))
    monkeypatch.setattr(maintenance_tasks, "SessionLocal", _no_close(db))


@pytest.fixture
def schedule_trigger(db, project):
    """A schedule trigger, due immediately — no network, unlike RSS or GitHub."""
    trigger = Trigger(
        project_id=project.id,
        kind=TriggerKind.SCHEDULE,
        name="Weekly piece",
        config={"every_hours": 168, "topic": "What shipped"},
        state={},
    )
    db.add(trigger)
    db.commit()
    db.refresh(trigger)
    return trigger


# --------------------------------------------------------------------------- #
# check_trigger                                                                #
# --------------------------------------------------------------------------- #


def test_checking_a_trigger_that_no_longer_exists_is_not_an_error(db):
    """The row was deleted between dispatch and pickup. Normal, not a failure.

    A raise here would burn the task's whole retry budget re-reading a row that
    is never coming back.
    """
    assert trigger_tasks.check_trigger(9999) == {
        "trigger_id": 9999,
        "status": "skipped",
    }


def test_a_paused_trigger_is_skipped_rather_than_polled(db, schedule_trigger):
    schedule_trigger.is_active = False
    db.commit()

    result = trigger_tasks.check_trigger(schedule_trigger.id)
    assert result["status"] == "skipped"
    # Skipped means untouched: a paused trigger must not have its clock moved,
    # or unpausing it would look like it had just been checked.
    db.refresh(schedule_trigger)
    assert schedule_trigger.last_checked_at is None


def test_a_trigger_that_crashes_is_reported_not_raised(
    db, schedule_trigger, monkeypatch
):
    """One trigger's bad day must not take the sweep down with it."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the feed parser exploded")

    monkeypatch.setattr(trigger_tasks.trigger_service, "check", _boom)

    result = trigger_tasks.check_trigger(schedule_trigger.id)
    assert result["status"] == "error"
    assert "exploded" in result["error"]


def test_a_trigger_that_times_out_says_so(db, schedule_trigger, monkeypatch):
    """The soft time limit is a verdict on this trigger, not on the worker."""

    def _slow(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(trigger_tasks.trigger_service, "check", _slow)

    result = trigger_tasks.check_trigger(schedule_trigger.id)
    assert result == {"trigger_id": schedule_trigger.id, "status": "timeout"}


def test_checking_a_due_schedule_trigger_fires_it(db, schedule_trigger):
    result = trigger_tasks.check_trigger(schedule_trigger.id)

    assert result["trigger_id"] == schedule_trigger.id
    # Autopilot is off on the default project, so the firing is recorded and
    # nothing is written — which is the outcome being asserted, not a failure.
    assert result["status"] in {
        TriggerEventStatus.SKIPPED.value,
        TriggerEventStatus.GENERATED.value,
    }
    assert db.query(TriggerEvent).count() == 1


# --------------------------------------------------------------------------- #
# check_due_triggers                                                           #
# --------------------------------------------------------------------------- #


def test_the_sweep_dispatches_every_due_trigger(db, schedule_trigger):
    result = trigger_tasks.check_due_triggers()
    assert result == {"due": 1, "dispatched": 1}


def test_the_sweep_skips_a_trigger_on_a_deactivated_account(
    db, schedule_trigger, user
):
    """Deactivating an account has to stop what it had already set running.

    The account cannot sign in to switch its own triggers off, so if the sweep
    kept picking them up they would keep writing and publishing with its stored
    platform credentials on a schedule nobody could reach.
    """
    user.is_active = False
    db.commit()

    assert trigger_tasks.check_due_triggers() == {"due": 0, "dispatched": 0}


def test_the_sweep_skips_a_paused_project(db, schedule_trigger, project):
    project.is_active = False
    db.commit()

    assert trigger_tasks.check_due_triggers() == {"due": 0, "dispatched": 0}


def test_the_sweep_ignores_inbound_webhook_triggers(db, project):
    """Webhooks arrive on a request thread; there is nothing to go and look at."""
    from app.services import triggers as trigger_service

    db.add(
        Trigger(
            project_id=project.id,
            kind=TriggerKind.WEBHOOK,
            name="Inbound",
            token=trigger_service.generate_token(),
            config={},
            state={},
        )
    )
    db.commit()

    assert trigger_tasks.check_due_triggers() == {"due": 0, "dispatched": 0}


def test_a_broker_that_refuses_the_dispatch_falls_back_to_running_inline(
    db, schedule_trigger, monkeypatch
):
    """Losing a poll because Redis is down would be a silent failure.

    The same fallback the repo scan makes, and the reason the return value
    counts dispatches rather than enqueues.
    """
    ran: list[int] = []

    def _broker_down(_trigger_id):
        raise ConnectionError("redis is not listening")

    monkeypatch.setattr(trigger_tasks.check_trigger, "delay", _broker_down)
    monkeypatch.setattr(
        trigger_tasks.trigger_service,
        "check",
        lambda _db, trigger: ran.append(trigger.id) or {"status": "no_news"},
    )

    assert trigger_tasks.check_due_triggers() == {"due": 1, "dispatched": 1}
    assert ran == [schedule_trigger.id]


def test_a_dispatch_that_runs_out_of_time_does_not_fall_back_to_inline(
    db, schedule_trigger, monkeypatch
):
    """The timeout must not be mistaken for a dead broker.

    ``SoftTimeLimitExceeded`` is an ``Exception``, so the fallback above caught
    it and responded the only way it knows — by running the check *inline*.
    That answers "you are out of time" with the most expensive call available,
    on a task already past its soft limit and heading for the hard one.
    """
    ran: list[int] = []

    def _too_slow(_trigger_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(trigger_tasks.check_trigger, "delay", _too_slow)
    monkeypatch.setattr(
        trigger_tasks.trigger_service,
        "check",
        lambda _db, trigger: ran.append(trigger.id) or {"status": "no_news"},
    )

    assert trigger_tasks.check_due_triggers() == {"due": 1, "dispatched": 0}
    assert ran == [], "the timeout must not trigger the broker-down fallback"


# --------------------------------------------------------------------------- #
# maintenance                                                                  #
# --------------------------------------------------------------------------- #


def test_purging_tokens_reports_what_it_removed(db, user):
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token("spent"),
            expires_at=utcnow() - timedelta(hours=2),
        )
    )
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token("live"),
            expires_at=utcnow() + timedelta(hours=2),
        )
    )
    db.commit()

    assert maintenance_tasks.purge_expired_tokens() == {"purged": 1}
    assert db.query(PasswordResetToken).count() == 1


@pytest.fixture
def endpoint(db, user):
    row = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        description="Slack",
        encrypted_secret="not-read-in-this-test",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _delivery(endpoint, *, status: DeliveryStatus, age_days: int) -> WebhookDelivery:
    return WebhookDelivery(
        webhook_id=endpoint.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload={"hello": "world"},
        status=status,
        created_at=utcnow() - timedelta(days=age_days),
    )


def test_old_settled_deliveries_are_purged(db, endpoint):
    db.add(_delivery(endpoint, status=DeliveryStatus.DELIVERED, age_days=400))
    db.add(_delivery(endpoint, status=DeliveryStatus.FAILED, age_days=400))
    db.commit()

    result = maintenance_tasks.purge_old_webhook_deliveries()
    assert result["purged"] == 2
    assert db.query(WebhookDelivery).count() == 0


def test_a_pending_delivery_survives_however_old_it_looks(db, endpoint):
    """A pending row is still owed an attempt.

    Deleting it on age would make a queue that quietly loses events under load —
    precisely when the backlog is oldest.
    """
    db.add(_delivery(endpoint, status=DeliveryStatus.PENDING, age_days=400))
    db.commit()

    assert maintenance_tasks.purge_old_webhook_deliveries() == {"purged": 0}
    assert db.query(WebhookDelivery).count() == 1


def test_a_recent_settled_delivery_survives(db, endpoint):
    """Kept long enough to answer "why didn't Slack hear about Tuesday's post"."""
    db.add(_delivery(endpoint, status=DeliveryStatus.DELIVERED, age_days=1))
    db.commit()

    assert maintenance_tasks.purge_old_webhook_deliveries() == {"purged": 0}


def _event(trigger, *, status: TriggerEventStatus, age_days: int) -> TriggerEvent:
    return TriggerEvent(
        trigger_id=trigger.id,
        headline="Something happened",
        payload={},
        status=status,
        created_at=utcnow() - timedelta(days=age_days),
    )


def test_old_settled_trigger_events_are_purged(db, schedule_trigger):
    for status in (
        TriggerEventStatus.GENERATED,
        TriggerEventStatus.SKIPPED,
        TriggerEventStatus.FAILED,
    ):
        db.add(_event(schedule_trigger, status=status, age_days=400))
    db.commit()

    assert maintenance_tasks.purge_old_trigger_events() == {"purged": 3}
    assert db.query(TriggerEvent).count() == 0


def test_a_received_trigger_event_is_kept(db, schedule_trigger):
    """A firing whose outcome was never recorded — a worker died mid-generation.

    That is exactly the row worth keeping until somebody has looked at it.
    """
    db.add(_event(schedule_trigger, status=TriggerEventStatus.RECEIVED, age_days=400))
    db.commit()

    assert maintenance_tasks.purge_old_trigger_events() == {"purged": 0}
    assert db.query(TriggerEvent).count() == 1


def test_a_recent_settled_trigger_event_is_kept(db, schedule_trigger):
    db.add(_event(schedule_trigger, status=TriggerEventStatus.GENERATED, age_days=1))
    db.commit()

    assert maintenance_tasks.purge_old_trigger_events() == {"purged": 0}


def test_the_autopilot_dispatch_does_not_scan_inline_when_it_runs_out_of_time(
    db, project, monkeypatch
):
    """The same trap as the trigger dispatch, with a costlier bottom.

    ``scan_project`` inline means reading a repository and calling a model. The
    broker-down fallback exists to make that trade deliberately; a soft-limit
    timeout falling through to it makes it by accident, at the worst moment.
    """
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", _no_close(db))
    scanned: list[int] = []

    class _TimesOutOnDispatch:
        """Stands in for the task: enqueueing times out, calling runs the scan."""

        def delay(self, _project_id):
            raise SoftTimeLimitExceeded()

        def __call__(self, project_id):
            scanned.append(project_id)

    monkeypatch.setattr(autopilot_tasks, "scan_project", _TimesOutOnDispatch())

    result = autopilot_tasks.scan_all_projects()

    assert result == {"scanned": 1, "dispatched": 0}
    assert scanned == [], "a timeout must not become an inline repo scan"
