"""The webhook delivery *tasks* — the sweep, not the service under it.

``webhooks.deliver`` is covered thoroughly by ``test_webhooks.py``. What was
not covered at all is ``deliver_due``: the periodic half of the delivery story,
which is the only thing that ever performs a retry. Every attempt after the
first one happens here, so the backoff, the isolation between endpoints and the
timeout escape are production-only paths that nothing exercised.

The tasks are called directly. ``.delay`` is Celery's to test, and with
``CELERY_ENABLED=false`` the call is synchronous anyway.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.models.mixins import utcnow
from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.services import link_check, webhooks
from app.tasks import webhook_tasks

_URL = "https://hooks.example.test/pulse"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Every hostname is public unless a test says otherwise.

    Without this the URL guard would consult the machine's resolver and the
    suite would behave differently on a laptop and in CI.
    """
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


def _no_close(session):
    """The test session, wrapped so a task's ``db.close()`` does not end it."""

    class NoCloseProxy:
        closed = 0

        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            type(self).closed += 1

    return NoCloseProxy


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    """Point the task module at the test database.

    Each task opens its own ``SessionLocal``, which otherwise resolves to the
    real engine and finds no tables.
    """
    proxy = _no_close(db)
    monkeypatch.setattr(webhook_tasks, "SessionLocal", proxy)
    return proxy


@pytest.fixture
def webhook(db, user) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url=_URL,
        description="Slack",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("shhh-a-secret-value"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _delivery(db, webhook: Webhook, *, due_in: timedelta | None = None) -> WebhookDelivery:
    """A pending delivery, due now unless *due_in* pushes it into the future."""
    event = WebhookEvent.CONTENT_PUBLISHED
    row = WebhookDelivery(
        webhook_id=webhook.id,
        event=event,
        payload=webhooks.envelope(event, {"hello": "world"}),
        status=DeliveryStatus.PENDING,
        next_attempt_at=utcnow() + (due_in or timedelta(0)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def receiver(monkeypatch):
    """An in-process endpoint that answers however the test tells it to."""

    class _Receiver:
        def __init__(self):
            self.status = 200
            self.body = ""
            self.error: Exception | None = None
            self.requests: list[httpx.Request] = []

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return httpx.Response(self.status, text=self.body)

    listener = _Receiver()
    monkeypatch.setattr(
        webhooks,
        "_http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(listener), follow_redirects=False
        ),
    )
    return listener


# --------------------------------------------------------------------------- #
# deliver_one                                                                  #
# --------------------------------------------------------------------------- #


def test_delivering_a_row_that_no_longer_exists_is_not_an_error(_task_session):
    """The endpoint was deleted between dispatch and pickup.

    The FK cascade takes its deliveries with it, so the id the worker is holding
    points at nothing. Raising would spend a Celery retry on a row that is never
    coming back.
    """
    assert webhook_tasks.deliver_one(9999) == {"delivery_id": 9999, "status": "gone"}
    assert _task_session.closed == 1, "the task must close the session it opened"


def test_delivering_one_row_reports_the_status_it_ended_up_in(db, webhook, receiver):
    delivery = _delivery(db, webhook)

    result = webhook_tasks.deliver_one(delivery.id)

    assert result == {"delivery_id": delivery.id, "status": "delivered"}
    assert len(receiver.requests) == 1


def test_a_refused_delivery_reports_its_status_rather_than_raising(
    db, webhook, receiver
):
    """A 500 from the endpoint is an outcome, not an exception.

    Letting it escape would add a Celery retry on top of the attempt counter the
    delivery row already keeps, and the two would race.
    """
    receiver.status = 500
    delivery = _delivery(db, webhook)

    result = webhook_tasks.deliver_one(delivery.id)

    assert result == {"delivery_id": delivery.id, "status": "pending"}
    db.refresh(delivery)
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None, "a retry must have been scheduled"


def test_a_failing_endpoints_own_words_are_stored_whole_but_bounded(
    db, webhook, receiver
):
    """``last_error`` quotes the remote server, which Pulse does not control.

    It is kept verbatim on purpose — a truncated or scrubbed excerpt is worse
    at the one job it has, which is telling the user what their endpoint said.
    What it must not do is grow without limit: the body is whatever the other
    end feels like sending, and it lands in a column shown on the Triggers
    page. Flattened to one line and capped, with the cap made visible.
    """
    receiver.status = 500
    receiver.body = "<script>alert(1)</script>\n" + "A" * 500
    delivery = _delivery(db, webhook)

    webhook_tasks.deliver_one(delivery.id)

    db.refresh(webhook)
    assert webhook.last_error is not None
    # Bounded: the 500-character tail cannot become a 500-character column.
    assert len(webhook.last_error) < 300
    assert webhook.last_error.endswith("…")
    # Verbatim within that bound: escaping belongs at the point of render, and
    # doing it here would show the user mangled text that is not what their
    # server sent. The frontend renders this as a JSX child — see
    # Triggers.test.jsx, "shows a hostile error message as text".
    assert "<script>alert(1)</script>" in webhook.last_error
    # Flattened, so a body full of newlines cannot smear the row.
    assert "\n" not in webhook.last_error


def test_a_short_error_body_is_not_marked_as_truncated(db, webhook, receiver):
    receiver.status = 500
    receiver.body = "no such tenant"
    delivery = _delivery(db, webhook)

    webhook_tasks.deliver_one(delivery.id)

    db.refresh(webhook)
    assert "no such tenant" in webhook.last_error
    assert "…" not in webhook.last_error


# --------------------------------------------------------------------------- #
# deliver_due — the sweep                                                      #
# --------------------------------------------------------------------------- #


def test_an_empty_backlog_sweeps_to_zero(_task_session):
    assert webhook_tasks.deliver_due() == {"attempted": 0, "delivered": 0}
    assert _task_session.closed == 1


def test_the_sweep_delivers_every_row_whose_backoff_has_elapsed(db, webhook, receiver):
    for _ in range(3):
        _delivery(db, webhook)

    assert webhook_tasks.deliver_due() == {"attempted": 3, "delivered": 3}
    assert len(receiver.requests) == 3


def test_the_sweep_leaves_a_row_whose_backoff_has_not_elapsed(db, webhook, receiver):
    """This is the whole point of the sweep: a retry waits its turn.

    A row parked an hour out by ``_record_failure`` must not be picked up by the
    pass that runs a minute later, or the backoff would not be a backoff.
    """
    due = _delivery(db, webhook)
    later = _delivery(db, webhook, due_in=timedelta(hours=1))

    assert webhook_tasks.deliver_due() == {"attempted": 1, "delivered": 1}

    db.refresh(due)
    db.refresh(later)
    assert due.status is DeliveryStatus.DELIVERED
    assert later.status is DeliveryStatus.PENDING
    assert later.attempts == 0


def test_a_refused_endpoint_counts_as_attempted_but_not_delivered(
    db, webhook, receiver
):
    receiver.status = 503
    _delivery(db, webhook)

    assert webhook_tasks.deliver_due() == {"attempted": 1, "delivered": 0}


def test_one_endpoints_bad_day_does_not_stop_the_rest_of_the_queue(
    db, webhook, monkeypatch
):
    """``deliver`` promises never to raise, but the sweep does not rely on it.

    If it ever does — a bug, a database error mid-commit — the deliveries behind
    it in the queue are for *other* people's endpoints, and they must still go
    out.
    """
    first = _delivery(db, webhook)
    second = _delivery(db, webhook)
    third = _delivery(db, webhook)
    seen: list[int] = []

    def _deliver(session, delivery):
        seen.append(delivery.id)
        if delivery.id == second.id:
            raise RuntimeError("commit blew up")
        return delivery

    monkeypatch.setattr(webhook_tasks.webhooks, "deliver", _deliver)

    result = webhook_tasks.deliver_due()

    assert seen == [first.id, second.id, third.id], "the sweep must not stop early"
    # Every row was attempted; none reached "delivered" because the stub never
    # sets a status.
    assert result == {"attempted": 3, "delivered": 0}


def test_a_sweep_that_runs_out_of_time_stops_instead_of_being_killed(
    db, webhook, monkeypatch
):
    """The soft limit arrives as an exception inside whichever delivery is in
    flight. Catching it lets the sweep return what it managed; ignoring it would
    let the hard limit kill the worker mid-commit.
    """
    first = _delivery(db, webhook)
    second = _delivery(db, webhook)
    third = _delivery(db, webhook)
    seen: list[int] = []

    def _deliver(session, delivery):
        seen.append(delivery.id)
        if delivery.id == second.id:
            raise SoftTimeLimitExceeded()
        delivery.status = DeliveryStatus.DELIVERED
        return delivery

    monkeypatch.setattr(webhook_tasks.webhooks, "deliver", _deliver)

    result = webhook_tasks.deliver_due()

    assert seen == [first.id, second.id], "the sweep must stop at the timeout"
    assert third.id not in seen
    assert result == {"attempted": 2, "delivered": 1}


def test_the_sweep_closes_its_session_even_when_the_query_fails(
    _task_session, monkeypatch
):
    """The ``finally`` is what stops a broken sweep leaking a connection every
    time beat fires it.
    """

    def _boom(session, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(webhook_tasks.webhooks, "due_deliveries", _boom)

    with pytest.raises(RuntimeError):
        webhook_tasks.deliver_due()

    assert _task_session.closed == 1
