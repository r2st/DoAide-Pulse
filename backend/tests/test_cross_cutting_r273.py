"""R273 cross-cutting concerns: timezone on content PATCH, clip_error in
headline sync, reject_nulls on update_me.

Three dimensions where a pattern was applied everywhere except one place:

1. **Timezone on content PATCH.**  ``POST /publish``, ``POST /schedule``,
   ``POST /bulk/publish`` and ``PATCH /calendar/content/{id}`` all accept a
   ``timezone`` parameter alongside ``scheduled_for``, feeding it through
   ``scheduling.normalize(…, tz=…)`` so a naive wall-clock time is read in
   the zone the user meant.  ``PATCH /content/{id}`` accepted
   ``scheduled_for`` but had no ``timezone`` field, so a naive datetime was
   always read as UTC — the one endpoint where a user editing a schedule
   could not say which clock they were using.

2. **clip_error in headline_sync.**  Every service that stores or returns an
   error string runs it through ``clip_error()`` (2 000 chars, with an
   ellipsis).  ``headline_sync`` used ``str(exc)[:300]`` — a magic number
   that disagreed with every other truncation site and lost the ellipsis.

3. **reject_nulls on update_me.**  Every PATCH endpoint that loops
   ``model_dump(exclude_unset=True)`` through ``setattr`` calls
   ``reject_nulls`` first so a ``null`` on a NOT NULL column is a 422 rather
   than a 500 or a silently-stored JSON null.  ``auth.update_me`` hand-rolled
   the same check for one field, leaving any future NOT NULL field unguarded.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import headline_sync
from app.services.errors import MAX_ERROR_CHARS, clip_error
from app.services.publishers.base import PublishError

V1 = "/api/v1"

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Cross-cutting test piece",
        slug="cross-cutting-test-piece",
        body_markdown="The body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# 1. timezone on content PATCH                                                 #
# --------------------------------------------------------------------------- #


def test_content_patch_accepts_timezone(client, auth, piece):
    """PATCH /content/{id} should accept a timezone alongside scheduled_for."""
    future = (datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None)
    resp = client.patch(
        f"{V1}/content/{piece.id}",
        headers=auth,
        json={
            "scheduled_for": future.isoformat(),
            "timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200, resp.text


def test_content_patch_timezone_shifts_naive_datetime(client, auth, piece, db):
    """A naive datetime with timezone='Asia/Kolkata' should not be stored as UTC."""
    wall_clock = datetime(2026, 12, 1, 9, 0)
    resp = client.patch(
        f"{V1}/content/{piece.id}",
        headers=auth,
        json={
            "scheduled_for": wall_clock.isoformat(),
            "timezone": "Asia/Kolkata",
        },
    )
    assert resp.status_code == 200, resp.text
    db.refresh(piece)
    stored = piece.scheduled_for.replace(tzinfo=UTC)
    expected = datetime(2026, 12, 1, 3, 30, tzinfo=UTC)
    assert stored == expected, f"expected {expected}, got {stored}"


def test_content_patch_timezone_ignored_when_offset_present(client, auth, piece, db):
    """An aware datetime should ignore the timezone field — the offset wins."""
    aware = datetime(2026, 12, 1, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    resp = client.patch(
        f"{V1}/content/{piece.id}",
        headers=auth,
        json={
            "scheduled_for": aware.isoformat(),
            "timezone": "US/Pacific",
        },
    )
    assert resp.status_code == 200, resp.text
    db.refresh(piece)
    stored = piece.scheduled_for.replace(tzinfo=UTC)
    expected = datetime(2026, 12, 1, 3, 30, tzinfo=UTC)
    assert stored == expected


def test_content_patch_bad_timezone_is_422(client, auth, piece):
    """An invalid timezone name should be refused."""
    future = (datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None)
    resp = client.patch(
        f"{V1}/content/{piece.id}",
        headers=auth,
        json={
            "scheduled_for": future.isoformat(),
            "timezone": "Fake/Nowhere",
        },
    )
    assert resp.status_code == 422


def test_content_patch_timezone_without_scheduled_for_is_harmless(client, auth, piece):
    """Sending timezone alone (no scheduled_for) should not error."""
    resp = client.patch(
        f"{V1}/content/{piece.id}",
        headers=auth,
        json={"title": "New title", "timezone": "Europe/Berlin"},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 2. clip_error in headline_sync                                               #
# --------------------------------------------------------------------------- #


def test_headline_sync_uses_clip_error_not_manual_truncation():
    """SyncOutcome.detail should be bounded by clip_error, not [:300]."""
    long_message = "x" * 5000
    outcome = headline_sync.SyncOutcome(
        publication_id=1,
        platform=Platform.DEVTO,
        status=headline_sync.FAILED,
        detail=clip_error(long_message),
    )
    assert len(outcome.detail) <= MAX_ERROR_CHARS + 1
    assert outcome.detail.endswith("…")


def test_headline_sync_credential_error_uses_clip_error(db, project):
    """When credentials fail, the detail should use clip_error, not [:300]."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Retitle me",
        slug="retitle-me",
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="12345",
        external_url="https://dev.to/test/retitle-me",
        live_title="Old Title",
    )
    db.add(pub)
    db.commit()
    db.refresh(content)

    long_error = "A" * 3000
    exc = PublishError(long_error)
    detail = clip_error(str(exc))
    assert len(detail) <= MAX_ERROR_CHARS + 1
    assert detail != str(exc)[:300]


# --------------------------------------------------------------------------- #
# 3. reject_nulls on update_me                                                #
# --------------------------------------------------------------------------- #


def test_update_me_rejects_null_on_not_null_column(client, auth, db, user):
    """weekly_digest_enabled is NOT NULL — null must be a 422."""
    resp = client.patch(
        f"{V1}/auth/me", headers=auth, json={"weekly_digest_enabled": None}
    )
    assert resp.status_code == 422
    db.refresh(user)
    assert user.weekly_digest_enabled is True


def test_update_me_allows_null_on_nullable_column(client, auth, db, user):
    """full_name IS nullable — null should clear it."""
    user.full_name = "Was Set"
    db.commit()
    resp = client.patch(f"{V1}/auth/me", headers=auth, json={"full_name": None})
    assert resp.status_code == 200
    db.refresh(user)
    assert user.full_name is None


def test_update_me_reject_nulls_error_message_names_the_field(client, auth):
    """The 422 should name which field(s) cannot be null."""
    resp = client.patch(
        f"{V1}/auth/me", headers=auth, json={"weekly_digest_enabled": None}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "weekly_digest_enabled" in detail
