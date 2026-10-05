"""H023 Pass 4 — state machine audit of webhook delivery and trigger event lifecycles.

Finding 1: ``requeue`` accepted PENDING deliveries, which could double-deliver
when a worker had already claimed the row.

Finding 2: ``_daily_count`` only counted GENERATED events, allowing concurrent
fires to race past the daily trigger limit.
"""
from __future__ import annotations

import pytest

from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services import triggers as trigger_service, webhooks


# ---- Fixtures -------------------------------------------------------------- #


@pytest.fixture
def webhook(db, user) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        description="test",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("test-secret"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _delivery(db, webhook_row: Webhook) -> WebhookDelivery:
    row = WebhookDelivery(
        webhook_id=webhook_row.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"x": 1}),
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- Finding 1: requeue guards against PENDING ----------------------------- #


def test_requeue_rejects_a_pending_delivery(db, webhook):
    """A delivery that is still PENDING (may be in-flight) cannot be requeued.

    Before the fix, ``requeue`` unconditionally reset the delivery to PENDING,
    which could overwrite a worker's claim lease and cause a double-delivery.
    """
    delivery = _delivery(db, webhook)
    assert delivery.status == DeliveryStatus.PENDING

    with pytest.raises(webhooks.RequeueError, match="still being attempted"):
        webhooks.requeue(db, delivery)

    # The delivery is unchanged.
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING


def test_requeue_accepts_a_failed_delivery(db, webhook):
    """The normal requeue path: a FAILED delivery is re-armed."""
    delivery = _delivery(db, webhook)
    delivery.status = DeliveryStatus.FAILED
    delivery.error = "something broke"
    delivery.attempts = 5
    db.commit()

    webhooks.requeue(db, delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempts == 0
    assert delivery.error is None


def test_requeue_accepts_a_delivered_delivery(db, webhook):
    """A user may want to replay an event that already delivered."""
    delivery = _delivery(db, webhook)
    delivery.status = DeliveryStatus.DELIVERED
    delivery.delivered_at = utcnow()
    db.commit()

    webhooks.requeue(db, delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.delivered_at is None


# ---- Finding 2: daily count includes RECEIVED events ----------------------- #


@pytest.fixture
def writing_project(db, project):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    return project


def _trigger(db, project, kind=TriggerKind.WEBHOOK, **config) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=kind,
        name="test trigger",
        config=config,
        state={},
        token=trigger_service.generate_token() if kind == TriggerKind.WEBHOOK else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_daily_count_includes_received_events(db, writing_project):
    """RECEIVED (in-flight) events count toward the daily limit.

    Before the fix, ``_daily_count`` only counted GENERATED events, so
    concurrent fires all saw a count of zero and raced past the limit.
    """
    trigger = _trigger(db, writing_project)

    # Manually insert events in both RECEIVED and GENERATED states.
    events = []
    for i, status in enumerate(
        [TriggerEventStatus.GENERATED, TriggerEventStatus.RECEIVED, TriggerEventStatus.RECEIVED]
    ):
        event = TriggerEvent(
            trigger_id=trigger.id,
            dedupe_key=f"count-test-{i}",
            headline=f"Event {i}",
            payload={},
            status=status,
        )
        db.add(event)
        events.append(event)
    db.commit()
    for e in events:
        db.refresh(e)

    # Without excluding self: all three count.
    count = trigger_service._daily_count(db, writing_project.id)
    assert count == 3, (
        f"Expected 3 (1 GENERATED + 2 RECEIVED), got {count}. "
        "RECEIVED events must count toward the daily limit."
    )

    # With self-exclusion: the caller's own event is excluded.
    count_excl = trigger_service._daily_count(
        db, writing_project.id, exclude_event_id=events[1].id
    )
    assert count_excl == 2, (
        f"Expected 2 (excluded one RECEIVED), got {count_excl}. "
        "The caller's own event must be excluded."
    )


def test_daily_count_excludes_skipped_and_failed(db, writing_project):
    """SKIPPED and FAILED events do not count — they produced no content."""
    trigger = _trigger(db, writing_project)

    for i, status in enumerate(
        [TriggerEventStatus.SKIPPED, TriggerEventStatus.FAILED, TriggerEventStatus.GENERATED]
    ):
        event = TriggerEvent(
            trigger_id=trigger.id,
            dedupe_key=f"excl-test-{i}",
            headline=f"Event {i}",
            payload={},
            status=status,
        )
        db.add(event)
    db.commit()

    count = trigger_service._daily_count(db, writing_project.id)
    assert count == 1, (
        f"Expected 1 (only GENERATED), got {count}. "
        "SKIPPED and FAILED events must not count."
    )
