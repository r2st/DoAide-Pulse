"""R154 — state machine correctness fixes.

Three bugs found in the state machine audit:

1. ``cancel_armed`` stamped ARCHIVED_ERROR on publications cancelled because
   the piece was demoted to draft or review, not archived.
2. ``cancel_publication`` did not guard PUBLISHING — a mid-flight publication
   could be cancelled out from under its worker.
3. ``reclaim_stuck`` did not call ``sync_content_status`` on the re-arm path,
   leaving the content at FAILED while a PENDING publication existed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="State machine audit piece",
        slug="state-machine-audit",
        body_markdown="# Audit\n\n" + ("word " * 200),
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------------ #
# Bug 1: cancel_armed uses the right reason per status               #
# ------------------------------------------------------------------ #


def test_cancel_armed_uses_archived_error_for_archived_content(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()

    content.status = ContentStatus.ARCHIVED
    publishing_service.cancel_armed(db, content)

    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.error == publishing_service.ARCHIVED_ERROR


def test_cancel_armed_uses_withdrawn_error_when_told(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()

    content.status = ContentStatus.DRAFT
    publishing_service.cancel_armed(
        db, content, reason=publishing_service.WITHDRAWN_ERROR
    )

    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.error == publishing_service.WITHDRAWN_ERROR
    assert "archived" not in pub.error.lower()


def test_demoting_to_draft_via_api_stamps_withdrawn_error(
    client, auth, db, project, content
):
    """The full path through _settle_status → cancel_armed."""
    pub = Publication(
        content_id=content.id,
        platform=Platform.MEDIUM,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=utcnow() + timedelta(days=2),
    )
    db.add(pub)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}",
        json={"status": "draft"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.error == publishing_service.WITHDRAWN_ERROR


def test_demoting_to_review_via_api_stamps_withdrawn_error(
    client, auth, db, project, content
):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}",
        json={"status": "review"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.error == publishing_service.WITHDRAWN_ERROR


def test_archiving_via_api_stamps_archived_error(
    client, auth, db, project, content
):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}",
        json={"status": "archived"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.error == publishing_service.ARCHIVED_ERROR


# ------------------------------------------------------------------ #
# Bug 2: cancel_publication must not cancel a PUBLISHING row         #
# ------------------------------------------------------------------ #


def test_cancel_publication_skips_publishing_row(db, content, monkeypatch):
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
    )
    db.add(pub)
    db.commit()

    result = publish_tasks.cancel_publication(pub.id)

    assert result["cancelled"] is False
    db.refresh(pub)
    assert pub.status == PublicationStatus.PUBLISHING


def test_cancel_publication_still_cancels_pending(db, content, monkeypatch):
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()

    result = publish_tasks.cancel_publication(pub.id)

    assert result["cancelled"] is True
    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED


def test_cancel_publication_still_cancels_scheduled(db, content, monkeypatch):
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=utcnow() + timedelta(days=2),
    )
    db.add(pub)
    db.commit()

    result = publish_tasks.cancel_publication(pub.id)

    assert result["cancelled"] is True
    db.refresh(pub)
    assert pub.status == PublicationStatus.CANCELLED


# ------------------------------------------------------------------ #
# Bug 3: reclaim_stuck syncs content status on re-arm                #
# ------------------------------------------------------------------ #


def _stuck(db, content, *, attempts: int = 1) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
        attempts=attempts,
    )
    db.add(row)
    db.commit()
    row.updated_at = utcnow() - timedelta(
        seconds=settings.publish_stuck_after_seconds + 60
    )
    db.commit()
    return row


def test_reclaim_stuck_re_arm_syncs_content_to_approved(db, content):
    """A re-armed publication means the piece is no longer terminal — content
    should be APPROVED, not stuck at FAILED."""
    content.status = ContentStatus.FAILED
    db.commit()

    pub = _stuck(db, content, attempts=1)

    assert publishing_service.reclaim_stuck(db) == 1

    db.refresh(pub)
    assert pub.status == PublicationStatus.PENDING

    db.refresh(content)
    assert content.status == ContentStatus.APPROVED


def test_reclaim_stuck_terminal_still_syncs_to_failed(db, content):
    """The terminal branch already called sync — content should be FAILED."""
    content.status = ContentStatus.APPROVED
    db.commit()

    pub = _stuck(db, content, attempts=settings.publish_max_retries)

    assert publishing_service.reclaim_stuck(db) == 1

    db.refresh(pub)
    assert pub.status == PublicationStatus.FAILED

    db.refresh(content)
    assert content.status == ContentStatus.FAILED
