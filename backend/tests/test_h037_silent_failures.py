"""H037: silent failure patterns that swallow errors without recording them.

Five patterns identified and fixed:

1. ``webhooks.deliver`` outer except — a crash after ``claim()`` succeeded
   left the delivery row with bumped attempts but no error recorded.
2. ``triggers.fire`` outer except — a crash in bookkeeping after generation
   succeeded returned ``None`` without marking the event as FAILED.
3. ``headline_sync.sync_title`` outer except — a crash (e.g. project is None)
   returned partial outcomes with no indication that syncing crashed.
4. ``publishing_service.collect_metrics`` — a non-rate-limit ``PublishError``
   was logged at INFO, hiding persistent adapter failures from operators.
5. ``engagement_alerts.evaluate`` latch commit failure — the log message lacked
   the engagement value and threshold needed to understand the lost crossing.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services import (
    content_pipeline,
    headline_sync,
    link_check,
    publishing_service,
    triggers,
    webhooks,
)
from app.services.signals import TriggerSignal


# --------------------------------------------------------------------------- #
# Shared fixtures                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _inline_worker(db, monkeypatch):
    from app.tasks import webhook_tasks

    monkeypatch.setattr(webhook_tasks, "SessionLocal", _no_close(db))


# --------------------------------------------------------------------------- #
# Fix 1: webhooks.deliver — crash after claim records failure on delivery row  #
# --------------------------------------------------------------------------- #


def _webhook(db, user) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url="https://hooks.example.test/h037",
        description="test",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("test-secret"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _delivery(db, wh: Webhook) -> WebhookDelivery:
    row = WebhookDelivery(
        webhook_id=wh.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"t": 1}),
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_deliver_crash_after_claim_records_failure(db, user, monkeypatch, caplog):
    """A crash in the recording phase must still mark the delivery as failed."""
    wh = _webhook(db, user)
    delivery = _delivery(db, wh)

    endpoint = httpx.MockTransport(lambda req: httpx.Response(200))
    monkeypatch.setattr(
        webhooks,
        "_http_client",
        lambda: httpx.Client(transport=endpoint, follow_redirects=False),
    )

    def exploding_record_success(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(webhooks, "_record_success", exploding_record_success)

    with caplog.at_level("ERROR"):
        result = webhooks.deliver(db, delivery)

    assert result is delivery
    assert delivery.error is not None
    assert "Internal error (RuntimeError)" in delivery.error
    assert "crashed" in caplog.text


# --------------------------------------------------------------------------- #
# Fix 2: triggers.fire — crash in bookkeeping marks event FAILED               #
# --------------------------------------------------------------------------- #


def _signal() -> TriggerSignal:
    return TriggerSignal(
        kind=TriggerKind.WEBHOOK,
        source="test",
        headline="Something happened",
        summary="Details.",
        dedupe_key="h037",
        suggested_type=ContentType.ANNOUNCEMENT,
    )


def _trigger(db, project, kind, **config) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=kind,
        name=f"{kind.value} trigger",
        config=config,
        state={},
    )
    if kind == TriggerKind.WEBHOOK:
        row.token = triggers.generate_token()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_fire_crash_in_bookkeeping_marks_event_failed(db, project, monkeypatch, caplog):
    """When bookkeeping after successful generation crashes, the event is FAILED."""
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()

    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    call_count = 0

    def succeed_then_crash_on_commit(session, *a, **kw):
        nonlocal call_count
        call_count += 1

        class FakeRouted:
            class content:
                id = 999

            auto_published = False
            status = "review"

        return FakeRouted()

    monkeypatch.setattr(content_pipeline, "generate_and_route", succeed_then_crash_on_commit)

    original_commit = db.commit.__func__ if hasattr(db.commit, '__func__') else None
    commit_calls = 0

    def crashing_commit(self=None):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 3:
            raise RuntimeError("unexpected bookkeeping crash")
        if original_commit:
            original_commit(db)
        else:
            type(db).commit(db)

    monkeypatch.setattr(type(db), "commit", crashing_commit)

    with caplog.at_level("ERROR"):
        event = triggers.fire(db, trigger, _signal())

    assert event is not None
    assert event.status == TriggerEventStatus.FAILED
    assert "Firing crashed" in (event.detail or "")


# --------------------------------------------------------------------------- #
# Fix 3: headline_sync.sync_title — crash appends a FAILED outcome            #
# --------------------------------------------------------------------------- #


def test_sync_title_crash_appends_failed_outcome(db, user, project, monkeypatch, caplog):
    """A crash mid-sync adds a FAILED outcome instead of silently returning partial."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="New Title",
        slug="h037-sync",
        body_markdown="body",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    def exploding_syncable(session, c):
        raise AttributeError("project is None")

    monkeypatch.setattr(headline_sync, "_syncable", exploding_syncable)

    with caplog.at_level("ERROR"):
        outcomes = headline_sync.sync_title(db, content)

    assert len(outcomes) == 1
    assert outcomes[0].status == headline_sync.FAILED
    assert "Sync crashed" in outcomes[0].detail
    assert "crashed" in caplog.text


# --------------------------------------------------------------------------- #
# Fix 4: publishing_service.collect_metrics — PublishError logged at WARNING    #
# --------------------------------------------------------------------------- #


def test_collect_metrics_publish_error_logged_at_warning(
    db, user, project, monkeypatch, caplog
):
    """A non-rate-limit PublishError is logged at WARNING, not INFO."""
    from app.services.publishers.base import PublishError

    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Metrics Test",
        slug="h037-metrics",
        body_markdown="body",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="ext-123",
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)

    from app.services import publishers as publishers_mod

    class FakeAdapter:
        supports_metrics = True

        def fetch_metrics(self, ext_id, creds):
            raise PublishError("adapter broken permanently")

    monkeypatch.setattr(publishers_mod, "get_adapter", lambda platform: FakeAdapter())
    monkeypatch.setattr(publishing_service, "_redact_credentials", lambda *a, **kw: None)

    from app.services.crypto import encrypt_credentials
    from app.models.platform_connection import PlatformConnection, ConnectionStatus

    conn = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@test",
    )
    db.add(conn)
    db.commit()

    with caplog.at_level("WARNING"):
        result = publishing_service.collect_metrics(db, pub, user_id=user.id)

    assert result is None
    assert any(
        r.levelname == "WARNING" and "failed" in r.message
        for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# Fix 5: engagement_alerts.evaluate — latch failure logs engagement context    #
# --------------------------------------------------------------------------- #


def test_evaluate_latch_failure_logs_engagement_and_threshold(
    db, user, project, monkeypatch, caplog
):
    """A failed latch commit logs both engagement and threshold values."""
    from app.models.metrics import ContentMetric
    from app.services import engagement_alerts

    project.engagement_threshold = 10
    db.commit()

    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Engagement Test",
        slug="h037-engage",
        body_markdown="body",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="ext-engage",
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)

    metric = ContentMetric(
        publication_id=pub.id,
        views=100,
        reactions=15,
    )
    db.add(metric)
    db.commit()

    original_commit = type(db).commit
    commit_count = 0

    def failing_commit(self):
        nonlocal commit_count
        commit_count += 1
        if commit_count == 1:
            raise RuntimeError("latch write failed")
        original_commit(self)

    monkeypatch.setattr(type(db), "commit", failing_commit)

    with caplog.at_level("ERROR"):
        crossings = engagement_alerts.evaluate(db, [content.id])

    assert len(crossings) == 0
    assert any(
        "engagement=" in r.message and "threshold=" in r.message
        for r in caplog.records
        if r.levelno >= 40
    )
