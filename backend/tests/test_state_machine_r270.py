"""R270 — state machine fixes for stuck-APPROVED and reclaim_stuck race.

Bug 1: Approving a FAILED piece leaves it stuck in APPROVED forever.
    ``release_approved`` declines a piece that already has publication rows,
    and a FAILED piece's terminal publications are still there. So the piece
    sits APPROVED with no path forward — no sweep picks it up, no worker
    touches it.

Bug 2: ``reclaim_stuck`` re-arms a publication to PENDING without calling
    ``arming_approves``. If the content was demoted to DRAFT or REVIEW while
    the publication was stuck in PUBLISHING, the re-armed row creates the
    invariant violation that ``arming_approves`` exists to prevent: a
    DRAFT piece with a PENDING publication, which the beat sweep then
    publishes.
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
        title="R270 piece",
        slug="r270-piece",
        body_markdown="# R270\n\n" + ("word " * 200),
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------------ #
# Bug 1: FAILED → APPROVED is refused                                #
# ------------------------------------------------------------------ #


def test_approve_endpoint_refuses_failed_content(client, db, content, auth):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        error="platform down",
    )
    db.add(pub)
    content.status = ContentStatus.FAILED
    db.commit()

    resp = client.post(f"/api/v1/content/{content.id}/approve", headers=auth)
    assert resp.status_code == 409
    assert "failed publications" in resp.json()["detail"].lower()

    db.refresh(content)
    assert content.status == ContentStatus.FAILED


def test_patch_refuses_failed_to_approved(client, db, content, auth):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        error="platform down",
    )
    db.add(pub)
    content.status = ContentStatus.FAILED
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}",
        json={"status": "approved"},
        headers=auth,
    )
    assert resp.status_code == 409

    db.refresh(content)
    assert content.status == ContentStatus.FAILED


def test_status_endpoint_refuses_failed_to_approved(client, db, content, auth):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        error="platform down",
    )
    db.add(pub)
    content.status = ContentStatus.FAILED
    db.commit()

    resp = client.post(
        f"/api/v1/content/{content.id}/status",
        json={"status": "approved"},
        headers=auth,
    )
    assert resp.status_code == 409

    db.refresh(content)
    assert content.status == ContentStatus.FAILED


def test_failed_to_draft_is_still_allowed(client, db, content, auth):
    """Moving a failed piece back to draft for reworking must still work."""
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        error="platform down",
    )
    db.add(pub)
    content.status = ContentStatus.FAILED
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}",
        json={"status": "draft"},
        headers=auth,
    )
    assert resp.status_code == 200

    db.refresh(content)
    assert content.status == ContentStatus.DRAFT


# ------------------------------------------------------------------ #
# Bug 2: reclaim_stuck calls arming_approves on re-arm                #
# ------------------------------------------------------------------ #


def test_reclaim_stuck_approves_draft_content_when_rearming(db, content):
    """A publication stuck in PUBLISHING on a piece demoted to DRAFT must
    move the content back to APPROVED when re-armed, not leave a DRAFT
    with a PENDING publication."""
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
        attempts=0,
    )
    db.add(pub)
    content.status = ContentStatus.DRAFT
    db.commit()

    stale = utcnow() - timedelta(seconds=settings.publish_stuck_after_seconds + 60)
    db.query(Publication).filter(Publication.id == pub.id).update(
        {Publication.updated_at: stale}
    )
    db.commit()
    db.expire_all()

    publishing_service.reclaim_stuck(db)

    db.refresh(content)
    db.refresh(pub)
    assert pub.status == PublicationStatus.PENDING
    assert content.status == ContentStatus.APPROVED


def test_reclaim_stuck_approves_review_content_when_rearming(db, content):
    """Same as above but with REVIEW status."""
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
        attempts=0,
    )
    db.add(pub)
    content.status = ContentStatus.REVIEW
    db.commit()

    stale = utcnow() - timedelta(seconds=settings.publish_stuck_after_seconds + 60)
    db.query(Publication).filter(Publication.id == pub.id).update(
        {Publication.updated_at: stale}
    )
    db.commit()
    db.expire_all()

    publishing_service.reclaim_stuck(db)

    db.refresh(content)
    db.refresh(pub)
    assert pub.status == PublicationStatus.PENDING
    assert content.status == ContentStatus.APPROVED
