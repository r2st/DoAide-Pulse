"""Cancelling a publication is a verdict on the piece, and says so.

``sync_content_status`` is asked after every publish attempt, and it was only
ever asked by the paths that *finish* a publication. Cancelling is the other way
a publication stops mattering, and neither path that does it — ``DELETE
/content/{id}/schedule`` or the ``cancel_publication`` task — re-derived
anything. So a piece could be left in a state no worker would ever touch again
while its status still said it was on its way:

    devto failed, medium scheduled, piece "approved"
    → unschedule → devto failed, medium cancelled, piece still "approved"

``publish_due`` sees no armed row, ``release_approved`` declines a piece that
has publications at all, and the retry sweep has nothing to reclaim. The only
thing still claiming the piece was going anywhere was the column.

The other half of the fix is what a cancelled row means to that derivation. It
used to count as terminal, which made cancelling *every* platform one call away
from reporting the piece ``failed`` — and nothing had failed. A cancelled row is
now excluded from the verdict entirely: the piece reads as though the row had
never been armed, which is what the user asked for by cancelling it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


@pytest.fixture
def worker(monkeypatch, task_session):
    """Let the background tasks run against the test's own session."""
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)
    return publish_tasks


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="A piece",
        slug="a-piece",
        body_markdown="Body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _pub(db, piece, platform, status, **kwargs):
    row = Publication(
        content_id=piece.id, platform=platform, status=status, **kwargs
    )
    db.add(row)
    db.commit()
    db.expire(piece, ["publications"])
    return row


# --------------------------------------------------------------------------- #
# What a cancelled row means to the derivation                                 #
# --------------------------------------------------------------------------- #


def test_cancelling_every_platform_is_not_a_failure(db, piece):
    """Nothing failed. The user said "not these platforms"."""
    _pub(db, piece, Platform.DEVTO, PublicationStatus.CANCELLED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.CANCELLED)

    publishing_service.sync_content_status(piece)

    assert piece.status == ContentStatus.APPROVED
    assert piece.published_at is None


def test_a_cancelled_row_does_not_hold_a_failure_open(db, piece):
    """The one live row failed terminally, so the piece failed.

    The old rule reached this answer too, but only because it counted the
    cancelled row as terminal alongside the failed one. Excluding it has to keep
    the answer, or fixing the case above would break this one.
    """
    _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED, error="nope")
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.CANCELLED)

    publishing_service.sync_content_status(piece)

    assert piece.status == ContentStatus.FAILED


def test_a_cancelled_row_does_not_hide_a_success(db, piece):
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.CANCELLED)

    publishing_service.sync_content_status(piece)

    assert piece.status == ContentStatus.PUBLISHED
    assert piece.published_at is not None


def test_a_cancelled_row_beside_one_still_in_flight_holds_the_verdict_open(db, piece):
    _pub(db, piece, Platform.DEVTO, PublicationStatus.CANCELLED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    publishing_service.sync_content_status(piece)

    assert piece.status == ContentStatus.APPROVED


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


def test_unscheduling_the_last_live_row_settles_a_failed_piece(
    db, client, auth, piece
):
    """The stuck state. Nothing would ever publish this, and nothing said so."""
    _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED, error="nope")
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.FAILED


def test_unscheduling_everything_leaves_the_piece_approved(db, client, auth, piece):
    """Taking a piece off the calendar is not a way of failing it."""
    _pub(db, piece, Platform.DEVTO, PublicationStatus.SCHEDULED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    piece = db.get(Content, piece.id)
    assert piece.status == ContentStatus.APPROVED
    assert piece.scheduled_for is None
    assert {p.status for p in piece.publications} == {PublicationStatus.CANCELLED}


def test_unscheduling_does_not_disturb_a_published_piece(db, client, auth, piece):
    """One platform live, one still parked. Unscheduling the copy changes nothing."""
    piece.status = ContentStatus.PUBLISHED
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.PUBLISHED


# --------------------------------------------------------------------------- #
# The task                                                                     #
# --------------------------------------------------------------------------- #


def test_the_cancel_task_settles_the_piece_too(db, worker, piece):
    """Same verdict, reached from the worker rather than from a request."""
    _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED, error="nope")
    parked = _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    assert worker.cancel_publication(parked.id)["cancelled"] is True

    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.FAILED


def test_the_cancel_task_does_not_fail_a_piece_it_merely_unqueued(db, worker, piece):
    only = _pub(db, piece, Platform.DEVTO, PublicationStatus.SCHEDULED)

    assert worker.cancel_publication(only.id)["cancelled"] is True

    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.APPROVED


def test_the_cancel_task_still_declines_a_terminal_row(db, worker, piece):
    done = _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)

    assert worker.cancel_publication(done.id)["cancelled"] is False

    db.expire_all()
    assert db.get(Publication, done.id).status == PublicationStatus.PUBLISHED


# --------------------------------------------------------------------------- #
# Recovery                                                                     #
# --------------------------------------------------------------------------- #


def test_a_settled_piece_can_still_be_published_again(db, client, auth, piece, connect):
    """``failed`` is not a dead end: re-publishing re-arms the row and the piece.

    Scheduled rather than immediate so the assertion is about the queue state
    and not about whether a platform Pulse cannot reach in tests answers.
    """
    connect(Platform.DEVTO)
    _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED, error="nope")
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)
    client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.FAILED

    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={
            "platforms": ["devto"],
            "allow_broken_links": True,
            "scheduled_for": (utcnow() + timedelta(days=1)).isoformat(),
        },
    )

    assert resp.status_code == 200, resp.text
    db.expire_all()
    piece = db.get(Content, piece.id)
    assert piece.status == ContentStatus.APPROVED
    assert {p.platform: p.status for p in piece.publications}[Platform.DEVTO] == (
        PublicationStatus.SCHEDULED
    )


def test_retrying_takes_a_piece_back_out_of_failed(db, client, auth, piece, connect):
    """A retry that a worker is about to run must not read as a failure.

    The syndication hold is what keeps this off the dispatch path: Medium is a
    copy of a piece whose canonical has not published, so the row is re-armed
    ``scheduled`` and the beat sweep owns it from there.
    """
    connect(Platform.DEVTO, Platform.MEDIUM)
    piece.project.auto_canonical = True
    piece.project.canonical_platform = Platform.DEVTO
    piece.status = ContentStatus.FAILED
    _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED, error="nope")
    copy = _pub(db, piece, Platform.MEDIUM, PublicationStatus.FAILED, error="nope")
    db.commit()

    resp = client.post(
        f"/api/v1/content/{piece.id}/retry/{copy.id}", headers=auth
    )

    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.APPROVED


def test_unscheduling_also_cancels_pending_publications(db, client, auth, piece):
    """A PENDING publication goes out on the next sweep — unschedule must stop it.

    Before the fix, ``unschedule_content`` only matched SCHEDULED rows. A
    PENDING row (the canonical platform when copies are syndication-delayed, or
    any row whose dispatch hasn't fired yet) was left armed and went out on the
    next beat sweep despite the user asking to unschedule.
    """
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PENDING)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.SCHEDULED)

    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text
    cancelled = {r["platform"]: r["status"] for r in resp.json()}
    assert cancelled == {"devto": "cancelled", "medium": "cancelled"}

    db.expire_all()
    for pub in db.get(Content, piece.id).publications:
        assert pub.status == PublicationStatus.CANCELLED
        assert pub.scheduled_for is None


def test_cancel_task_clears_scheduled_for(db, worker, piece):
    """A cancelled publication must not linger on the calendar.

    Before the fix, ``cancel_publication`` set status to CANCELLED but left
    ``scheduled_for`` set. Every other terminal path clears it; the omission
    here left a ghost entry on the calendar and in "upcoming" listings.
    """
    future = utcnow() + timedelta(days=3)
    row = _pub(db, piece, Platform.DEVTO, PublicationStatus.SCHEDULED)
    row.scheduled_for = future
    db.commit()

    assert worker.cancel_publication(row.id)["cancelled"] is True

    db.expire_all()
    pub = db.get(Publication, row.id)
    assert pub.status == PublicationStatus.CANCELLED
    assert pub.scheduled_for is None


def test_re_arming_does_not_promote_a_draft(db, piece):
    """``failed`` is the only status this arm takes back."""
    piece.status = ContentStatus.DRAFT
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PENDING)

    publishing_service.sync_content_status(piece)

    assert piece.status == ContentStatus.DRAFT
