"""H038 — state machine fixes for approval guards and trigger event races.

Finding 1: ``approve_content``, ``bulk_approve_content``, ``set_content_status``
and the PATCH endpoint all allowed ARCHIVED → APPROVED without refusing, which
contradicts the invariant enforced by the retry and queue-publish paths: "an
archived piece has nothing armed, ever."

Finding 2: ``reclaim_stuck_events`` loaded RECEIVED events and unconditionally
overwrote their status, racing with ``fire()`` which could change the status
between the SELECT and the commit.

Finding 3: ``requeue`` in webhooks.py allowed re-queuing a DELIVERED webhook
delivery without checking whether the endpoint was still active, meaning a
deactivated endpoint could receive a redelivery.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services import triggers as trigger_service, webhooks

API = "/api/v1/content"


# ---- Fixtures -------------------------------------------------------------- #


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="H038 state machine piece",
        slug="h038-state-machine",
        body_markdown="# Test\n\n" + ("word " * 200),
        status=ContentStatus.ARCHIVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def trigger(db, project) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.SCHEDULE,
        name="test-schedule",
        config={"every_hours": 1, "topic": "test"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def webhook_row(db, user) -> Webhook:
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


# ================================================================== #
# Finding 1: ARCHIVED → APPROVED is refused on all approval paths    #
# ================================================================== #


def test_approve_endpoint_rejects_archived_content(client, auth, content, db):
    """The single-item approve must refuse an archived piece."""
    resp = client.post(f"{API}/{content.id}/approve", headers=auth)
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()
    db.refresh(content)
    assert content.status == ContentStatus.ARCHIVED


def test_bulk_approve_skips_archived_content(client, auth, content, db):
    """Bulk approve must report archived pieces as failures."""
    resp = client.post(
        f"{API}/bulk/approve",
        json={"content_ids": [content.id]},
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert content.id not in body["succeeded"]
    assert any(f["content_id"] == content.id for f in body["failed"])
    db.refresh(content)
    assert content.status == ContentStatus.ARCHIVED


def test_set_status_endpoint_rejects_archived_to_approved(client, auth, content, db):
    """POST /content/{id}/status must refuse ARCHIVED → APPROVED."""
    resp = client.post(
        f"{API}/{content.id}/status",
        json={"status": "approved"},
        headers=auth,
    )
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()
    db.refresh(content)
    assert content.status == ContentStatus.ARCHIVED


def test_patch_rejects_archived_to_approved(client, auth, content, db):
    """PATCH /content/{id} with status=approved must refuse an archived piece."""
    resp = client.patch(
        f"{API}/{content.id}",
        json={"status": "approved"},
        headers={**auth, "If-Match": "*"},
    )
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"].lower()
    db.refresh(content)
    assert content.status == ContentStatus.ARCHIVED


def test_set_status_allows_archived_to_draft(client, auth, content, db):
    """ARCHIVED → DRAFT is still allowed (un-archiving to rework)."""
    resp = client.post(
        f"{API}/{content.id}/status",
        json={"status": "draft"},
        headers=auth,
    )
    assert resp.status_code == 200
    db.refresh(content)
    assert content.status == ContentStatus.DRAFT


# ================================================================== #
# Finding 2: reclaim_stuck_events uses conditional UPDATE             #
# ================================================================== #


def test_reclaim_stuck_events_skips_non_received_events(db, trigger):
    """An event that fire() already settled must not be overwritten by reclaim."""
    event = TriggerEvent(
        trigger_id=trigger.id,
        dedupe_key="already-handled",
        headline="Settled event",
        payload={},
        status=TriggerEventStatus.GENERATED,
    )
    db.add(event)
    db.commit()
    event_id = event.id

    past = utcnow() - timedelta(seconds=settings.trigger_event_stuck_after_seconds + 60)
    db.execute(
        TriggerEvent.__table__.update()
        .where(TriggerEvent.__table__.c.id == event_id)
        .values(created_at=past)
    )
    db.commit()
    db.expire_all()

    count = trigger_service.reclaim_stuck_events(db)
    assert count == 0

    db.expire_all()
    refreshed = db.get(TriggerEvent, event_id)
    assert refreshed.status == TriggerEventStatus.GENERATED


def test_reclaim_stuck_events_settles_genuinely_stuck_received(db, trigger):
    """A genuinely stuck RECEIVED event should still be settled."""
    event = TriggerEvent(
        trigger_id=trigger.id,
        dedupe_key=None,
        headline="Stuck event",
        payload={},
        status=TriggerEventStatus.RECEIVED,
    )
    db.add(event)
    db.commit()
    event_id = event.id

    past = utcnow() - timedelta(seconds=settings.trigger_event_stuck_after_seconds + 60)
    db.execute(
        TriggerEvent.__table__.update()
        .where(TriggerEvent.__table__.c.id == event_id)
        .values(created_at=past)
    )
    db.commit()
    db.expire_all()

    count = trigger_service.reclaim_stuck_events(db)
    assert count == 1

    db.expire_all()
    refreshed = db.get(TriggerEvent, event_id)
    assert refreshed.status == TriggerEventStatus.FAILED
    assert trigger_service.ABANDONED_DETAIL in refreshed.detail


def test_reclaim_does_not_touch_skipped_event(db, trigger):
    """A SKIPPED event older than the cutoff must not be reclaimed."""
    event = TriggerEvent(
        trigger_id=trigger.id,
        dedupe_key="skipped-one",
        headline="Skipped",
        payload={},
        status=TriggerEventStatus.SKIPPED,
        detail="Project paused.",
    )
    db.add(event)
    db.commit()
    event_id = event.id

    past = utcnow() - timedelta(seconds=settings.trigger_event_stuck_after_seconds + 60)
    db.execute(
        TriggerEvent.__table__.update()
        .where(TriggerEvent.__table__.c.id == event_id)
        .values(created_at=past)
    )
    db.commit()
    db.expire_all()

    count = trigger_service.reclaim_stuck_events(db)
    assert count == 0

    db.expire_all()
    refreshed = db.get(TriggerEvent, event_id)
    assert refreshed.status == TriggerEventStatus.SKIPPED


# ================================================================== #
# Finding 3: requeue checks endpoint active state                    #
# ================================================================== #


def test_requeue_refuses_delivery_to_inactive_endpoint(db, webhook_row):
    """Re-queuing a delivery to a deactivated endpoint must be refused."""
    delivery = WebhookDelivery(
        webhook_id=webhook_row.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"x": 1}),
        status=DeliveryStatus.FAILED,
        error="Connection refused",
        attempts=3,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    webhook_row.is_active = False
    db.commit()

    with pytest.raises(webhooks.RequeueError, match="switched off"):
        webhooks.requeue(db, delivery)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED


def test_requeue_allows_delivery_to_active_endpoint(db, webhook_row):
    """Re-queuing a delivery to an active endpoint must succeed."""
    delivery = WebhookDelivery(
        webhook_id=webhook_row.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"x": 1}),
        status=DeliveryStatus.FAILED,
        error="Connection refused",
        attempts=3,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    result = webhooks.requeue(db, delivery)
    assert result.status == DeliveryStatus.PENDING
    assert result.attempts == 0
