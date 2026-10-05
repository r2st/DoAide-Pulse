"""H024 cross-cutting concerns: content PATCH scheduled_for must reset
publication attempts and error, matching calendar reschedule behaviour.

The calendar's ``PATCH /calendar/content/{id}`` resets ``attempts = 0`` and
``error = None`` on every publication it moves, with the comment "A move is
a fresh start: a row that had burned two retries should not arrive at its new
slot with one left."  The content ``PATCH /content/{id}`` moved the same
publications to a new ``scheduled_for`` without resetting either field.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus

V1 = "/api/v1"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def piece_with_publications(db, project):
    """A piece with two publications, one of which has burned retries."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="H024 reschedule piece",
        slug="h024-reschedule-piece",
        body_markdown="The body.",
        scheduled_for=datetime.now(UTC) + timedelta(hours=6),
    )
    db.add(row)
    db.flush()

    future = datetime.now(UTC) + timedelta(hours=6)
    pub_ok = Publication(
        content_id=row.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=future,
        attempts=0,
        error=None,
    )
    pub_retried = Publication(
        content_id=row.id,
        platform=Platform.HASHNODE,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=future,
        attempts=2,
        error="Platform returned 503 — retrying in 120s",
    )
    db.add_all([pub_ok, pub_retried])
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_content_patch_scheduled_for_resets_attempts(
    client, auth, piece_with_publications, db
):
    """PATCH /content/{id} with a new scheduled_for should reset attempts to 0.

    The calendar's reschedule explicitly does this — a rescheduled publication
    is a fresh start and should not arrive at the new slot with a reduced
    retry budget.
    """
    new_time = datetime.now(UTC) + timedelta(hours=24)
    resp = client.patch(
        f"{V1}/content/{piece_with_publications.id}",
        headers=auth,
        json={"scheduled_for": new_time.isoformat()},
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    for pub in piece_with_publications.publications:
        if pub.status in (PublicationStatus.PENDING, PublicationStatus.SCHEDULED):
            assert pub.attempts == 0, (
                f"Publication {pub.id} ({pub.platform}) should have attempts=0 "
                f"after rescheduling, got {pub.attempts}"
            )


def test_content_patch_scheduled_for_clears_error(
    client, auth, piece_with_publications, db
):
    """PATCH /content/{id} with a new scheduled_for should clear stale errors.

    A publication moved to a new time should not carry an error message from
    an attempt at its previous time — the error is about the old slot, and
    showing it next to the new one is misleading.
    """
    new_time = datetime.now(UTC) + timedelta(hours=24)
    resp = client.patch(
        f"{V1}/content/{piece_with_publications.id}",
        headers=auth,
        json={"scheduled_for": new_time.isoformat()},
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    for pub in piece_with_publications.publications:
        if pub.status in (PublicationStatus.PENDING, PublicationStatus.SCHEDULED):
            assert pub.error is None, (
                f"Publication {pub.id} ({pub.platform}) should have error=None "
                f"after rescheduling, got {pub.error!r}"
            )


def test_content_patch_scheduled_for_does_not_touch_settled_publications(
    client, auth, piece_with_publications, db
):
    """Published or failed publications should not be affected by rescheduling."""
    settled_pub = Publication(
        content_id=piece_with_publications.id,
        platform=Platform.WORDPRESS,
        status=PublicationStatus.PUBLISHED,
        published_at=datetime.now(UTC) - timedelta(hours=1),
        attempts=1,
        error=None,
    )
    db.add(settled_pub)
    db.commit()
    db.refresh(settled_pub)

    new_time = datetime.now(UTC) + timedelta(hours=24)
    resp = client.patch(
        f"{V1}/content/{piece_with_publications.id}",
        headers=auth,
        json={"scheduled_for": new_time.isoformat()},
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    db.refresh(settled_pub)
    assert settled_pub.attempts == 1
    assert settled_pub.status == PublicationStatus.PUBLISHED
