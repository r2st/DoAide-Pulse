"""Archiving a piece stops it going out, which it did not used to.

``ARCHIVED`` is the one status that means "not going out" — it is what the
review queue's Reject button sets, what `PATCH /content/{id}` accepts on a piece
that is otherwise frozen, and what the docstrings call "stop showing me this".
It set a column and nothing else. The publications stayed armed, the beat sweep
came round on the scheduled day, and the piece the user had rejected was posted
to the internet.

`DELETE /content/{id}/schedule` was the only path that took a piece off the
queue, and it is the one a user reaches by knowing to look for it. Both
archiving paths now do the same thing, and `publishing_service.execute` refuses
an archived piece as well — the endpoints cannot cover the row a worker was
already holding when the piece was archived, and that window is as long as an
HTTP call to a platform.

Anything already live stays live: Herald cannot unpublish, and rewriting a
published row would only lose the record of where the post is.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Going out Friday",
        slug="going-out-friday",
        body_markdown="Body.",
        scheduled_for=utcnow() + timedelta(days=3),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _pub(db, piece, platform, status, *, when=None):
    row = Publication(
        content_id=piece.id,
        platform=platform,
        status=status,
        scheduled_for=when,
    )
    db.add(row)
    db.commit()
    db.expire(piece, ["publications"])
    return row


def _due_next_week(db):
    """What the beat sweep would pick up once the scheduled day arrives."""
    return publishing_service.due_publications(db, now=utcnow() + timedelta(days=7))


# --------------------------------------------------------------------------- #
# PATCH /content/{id}                                                          #
# --------------------------------------------------------------------------- #


def test_archiving_takes_the_scheduled_publication_off_the_sweep(
    db, client, auth, piece
):
    _pub(
        db,
        piece,
        Platform.DEVTO,
        PublicationStatus.SCHEDULED,
        when=utcnow() + timedelta(days=3),
    )

    resp = client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"status": "archived"}
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert _due_next_week(db) == []
    piece = db.get(Content, piece.id)
    assert piece.scheduled_for is None
    assert piece.publications[0].status == PublicationStatus.CANCELLED
    assert piece.publications[0].error == publishing_service.ARCHIVED_ERROR


def test_archiving_takes_a_pending_publication_too(db, client, auth, piece):
    """``pending`` means "go out on the next sweep", which is sooner, not later."""
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PENDING)

    client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"status": "archived"}
    )

    db.expire_all()
    assert publishing_service.due_publications(db) == []


def test_archiving_leaves_a_published_copy_alone(db, client, auth, piece):
    """Herald cannot unpublish, and the row is the record of where the post is."""
    live = _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _pub(
        db,
        piece,
        Platform.MEDIUM,
        PublicationStatus.SCHEDULED,
        when=utcnow() + timedelta(days=3),
    )
    piece.status = ContentStatus.PUBLISHED
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"status": "archived"}
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    by_platform = {p.platform: p for p in db.get(Content, piece.id).publications}
    assert by_platform[Platform.DEVTO].status == PublicationStatus.PUBLISHED
    assert by_platform[Platform.MEDIUM].status == PublicationStatus.CANCELLED
    assert _due_next_week(db) == []
    assert db.get(Publication, live.id).error is None


def test_a_failed_row_is_not_rewritten_by_archiving(db, client, auth, piece):
    """Terminal is terminal — the error on it is why the piece never went out."""
    failed = _pub(db, piece, Platform.DEVTO, PublicationStatus.FAILED)
    failed.error = "Dev.to rejected the credentials"
    db.commit()

    client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"status": "archived"}
    )

    db.expire_all()
    row = db.get(Publication, failed.id)
    assert row.status == PublicationStatus.FAILED
    assert row.error == "Dev.to rejected the credentials"


def test_editing_an_already_archived_piece_keeps_the_queue_in_step(
    db, client, auth, piece
):
    """The column and the queue must agree however the piece got here."""
    piece.status = ContentStatus.ARCHIVED
    db.commit()
    _pub(
        db,
        piece,
        Platform.DEVTO,
        PublicationStatus.SCHEDULED,
        when=utcnow() + timedelta(days=3),
    )

    resp = client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"title": "A new title"}
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert _due_next_week(db) == []


# --------------------------------------------------------------------------- #
# POST /content/bulk/reject                                                    #
# --------------------------------------------------------------------------- #


def test_rejecting_a_piece_does_not_then_publish_it(db, client, auth, piece):
    """The button is called Reject. It was posting the rejected piece."""
    _pub(
        db,
        piece,
        Platform.DEVTO,
        PublicationStatus.SCHEDULED,
        when=utcnow() + timedelta(days=3),
    )

    resp = client.post(
        "/api/v1/content/bulk/reject",
        headers=auth,
        json={"content_ids": [piece.id]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["succeeded"] == [piece.id]

    db.expire_all()
    assert _due_next_week(db) == []


# --------------------------------------------------------------------------- #
# The worker, for the row that was already in flight                           #
# --------------------------------------------------------------------------- #


def test_a_worker_holding_a_row_will_not_publish_an_archived_piece(db, piece, connect):
    """The claim is a conditional UPDATE over ``publications`` and cannot see this.

    Reproduces the race directly: the row is claimed, the piece is archived, and
    only then does ``execute`` run. Without the gate the adapter is called and
    the post goes out.
    """
    connect(Platform.DEVTO)
    publication = _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHING)
    piece.status = ContentStatus.ARCHIVED
    db.commit()

    publishing_service.execute(db, publication)

    db.refresh(publication)
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.error == publishing_service.ARCHIVED_ERROR
    assert publication.published_at is None
    # Not counted as an attempt: nothing was attempted.
    assert publication.attempts == 0


def test_the_gate_does_not_settle_the_piece_as_failed(db, piece, connect):
    """Cancelled is not failed, and an archived piece stays archived."""
    connect(Platform.DEVTO)
    publication = _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHING)
    piece.status = ContentStatus.ARCHIVED
    db.commit()

    publishing_service.execute(db, publication)

    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.ARCHIVED


# --------------------------------------------------------------------------- #
# cancel_armed itself                                                          #
# --------------------------------------------------------------------------- #


def test_cancel_armed_reports_what_it_cancelled(db, piece):
    _pub(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _pub(db, piece, Platform.MEDIUM, PublicationStatus.PENDING)

    cancelled = publishing_service.cancel_armed(db, piece)

    assert [p.platform for p in cancelled] == [Platform.MEDIUM]


def test_cancel_armed_on_a_piece_with_nothing_queued_is_a_no_op(db, piece):
    assert publishing_service.cancel_armed(db, piece) == []
