"""Every door that lets a caller name a status leaves the queue agreeing with it.

The content lifecycle is ``draft → review → approved → published`` with two
exits, and the column is written from eight places. Three of them had rules the
others did not, so the state machine was three different machines depending on
which endpoint the caller happened to use:

* **Demoting an approved piece left its queue armed.** Archiving cancels the
  armed rows, because the review queue's Reject once published the piece it had
  just rejected. Moving a piece back to ``draft`` or ``review`` — "not this, not
  yet" — set the column and stopped. ``due_publications`` selects on the row's
  status and time alone, and ``execute``'s last gate asks only whether the piece
  is archived, so a piece pulled back to draft on Monday to be reworked went
  out on Tuesday with whatever the body said by then.

* **Archiving a live piece and un-archiving it did in two calls what one
  refuses.** ``PATCH {"status": "draft"}`` on a published piece is a 409; the
  check read the column, archiving moves the column, and the second PATCH went
  through. The piece was then off the RSS feed, out of the published count and
  invisible to the headline sweep while its post was up on Dev.to.

* **The worker's outcome could be lost to an autosave.** ``Content`` has a
  ``version_id_col``; the worker loads the piece, spends seconds in a platform
  call, then commits ``published`` and the URL against the version it loaded.
  An editor autosaving the (approved, not yet frozen) piece in that window made
  the commit raise ``StaleDataError``, the record of the post rolled back with
  it, and ``reclaim_stuck`` later re-armed a row whose post was already live.

One helper now runs after every hand-set status (``_settle_status``), every
path that arms a row approves the piece it arms (``arming_approves``), and the
worker records its outcome against whatever the row has become
(``_commit_outcome``).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service
from app.services.publishers.base import PublishError, PublishResult
from app.services.publishers.devto import DevToAdapter

API = "/api/v1/content"
LIVE_URL = "https://dev.to/pulse/live-piece"


def _future(days: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _piece(db, project, status: ContentStatus, *rows) -> Content:
    piece = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=status,
        title="A piece",
        slug=f"a-piece-{status.value}",
        body_markdown="# A piece\n\nA body long enough to look like a post.",
        published_at=(
            utcnow()
            if status == ContentStatus.PUBLISHED
            or any(r == PublicationStatus.PUBLISHED for _, r in rows)
            else None
        ),
        scheduled_for=datetime.now(UTC) + timedelta(days=2) if rows else None,
    )
    db.add(piece)
    db.commit()
    for platform, row_status in rows:
        live = row_status == PublicationStatus.PUBLISHED
        armed = row_status in (PublicationStatus.SCHEDULED, PublicationStatus.PENDING)
        db.add(
            Publication(
                content_id=piece.id,
                platform=platform,
                status=row_status,
                published_at=utcnow() if live else None,
                external_url=LIVE_URL if live else None,
                scheduled_for=(
                    datetime.now(UTC) + timedelta(days=2)
                    if row_status == PublicationStatus.SCHEDULED
                    else None
                ),
                error=None if armed or live else "boom",
            )
        )
    db.commit()
    db.refresh(piece)
    return piece


def _rows(db, piece) -> list[Publication]:
    db.expire_all()
    return list(
        db.scalars(select(Publication).where(Publication.content_id == piece.id))
    )


def _move(client, auth, piece, via: str, to: str):
    if via == "patch":
        return client.patch(f"{API}/{piece.id}", json={"status": to}, headers=auth)
    return client.post(f"{API}/{piece.id}/status", json={"status": to}, headers=auth)


# --------------------------------------------------------------------------- #
# Demoting takes the piece off the queue                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("via", ["patch", "status"])
@pytest.mark.parametrize("to", ["draft", "review"])
def test_demoting_an_approved_piece_cancels_what_was_armed(
    client, auth, db, project, via, to
):
    piece = _piece(
        db,
        project,
        ContentStatus.APPROVED,
        (Platform.DEVTO, PublicationStatus.SCHEDULED),
        (Platform.MEDIUM, PublicationStatus.PENDING),
    )

    resp = _move(client, auth, piece, via, to)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == to
    assert resp.json()["scheduled_for"] is None
    rows = _rows(db, piece)
    assert {r.status for r in rows} == {PublicationStatus.CANCELLED}
    assert all(r.scheduled_for is None for r in rows)
    # And nothing is due: the sweep would otherwise publish the draft.
    assert publishing_service.due_publications(
        db, now=datetime.now(UTC) + timedelta(days=3)
    ) == []


def test_demoting_leaves_a_terminal_row_alone(client, auth, db, project):
    """A failed row is a record; cancelling it would rewrite history."""
    piece = _piece(
        db,
        project,
        ContentStatus.FAILED,
        (Platform.DEVTO, PublicationStatus.FAILED),
    )

    resp = _move(client, auth, piece, "patch", "draft")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    assert _rows(db, piece)[0].status == PublicationStatus.FAILED


def test_a_date_and_a_demotion_in_one_patch_end_with_no_date(
    client, auth, db, project
):
    """The ``scheduled_for`` pass re-arms the rows at the new time; the settle
    runs after it, so the demotion wins — the same order archiving already had."""
    piece = _piece(
        db, project, ContentStatus.APPROVED, (Platform.DEVTO, PublicationStatus.SCHEDULED)
    )

    resp = client.patch(
        f"{API}/{piece.id}",
        json={"status": "draft", "scheduled_for": _future(days=5)},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["scheduled_for"] is None
    assert _rows(db, piece)[0].status == PublicationStatus.CANCELLED


# --------------------------------------------------------------------------- #
# Un-archiving a piece that went out                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def archived_live(db, project) -> Content:
    """Published on Dev.to, then archived. Still up on Dev.to."""
    return _piece(
        db,
        project,
        ContentStatus.ARCHIVED,
        (Platform.DEVTO, PublicationStatus.PUBLISHED),
        (Platform.MEDIUM, PublicationStatus.CANCELLED),
    )


@pytest.mark.parametrize("via", ["patch", "status"])
@pytest.mark.parametrize("to", ["draft", "review", "approved"])
def test_un_archiving_a_live_piece_restores_published(
    client, auth, db, archived_live, via, to
):
    """Two calls must not do what one is refused. The piece comes back where
    the rows say it is."""
    resp = _move(client, auth, archived_live, via, to)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
    db.refresh(archived_live)
    assert archived_live.status == ContentStatus.PUBLISHED
    assert archived_live.published_at is not None
    # And the one move a published piece may make is still refused after it.
    assert _move(client, auth, archived_live, via, "draft").status_code == 409


def test_a_restored_piece_is_still_frozen(client, auth, db, archived_live):
    _move(client, auth, archived_live, "patch", "draft")
    resp = client.patch(
        f"{API}/{archived_live.id}", json={"body_markdown": "rewritten"}, headers=auth
    )
    assert resp.status_code == 409


def test_a_restored_piece_is_back_on_the_feed(client, auth, db, project, archived_live):
    """The reason it matters: the feed reads the column."""
    slug = archived_live.slug
    assert slug not in client.get(f"/api/v1/projects/{project.id}/feed.xml").text
    _move(client, auth, archived_live, "patch", "approved")
    assert slug in client.get(f"/api/v1/projects/{project.id}/feed.xml").text


def test_approving_an_archived_live_piece_restores_it_too(
    client, auth, db, archived_live
):
    resp = client.post(f"{API}/{archived_live.id}/approve", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"


def test_bulk_approving_an_archived_live_piece_restores_it_too(
    client, auth, db, archived_live
):
    resp = client.post(
        f"{API}/bulk/approve", json={"content_ids": [archived_live.id]}, headers=auth
    )
    assert resp.status_code == 200, resp.text
    db.refresh(archived_live)
    assert archived_live.status == ContentStatus.PUBLISHED


@pytest.mark.parametrize("to", ["draft", "review"])
def test_un_archiving_a_piece_that_never_went_out_goes_where_it_was_sent(
    client, auth, db, project, to
):
    piece = _piece(
        db,
        project,
        ContentStatus.ARCHIVED,
        (Platform.DEVTO, PublicationStatus.CANCELLED),
        (Platform.MEDIUM, PublicationStatus.FAILED),
    )
    resp = _move(client, auth, piece, "patch", to)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == to


def test_un_archiving_a_piece_that_never_went_out_requires_draft_before_approve(
    client, auth, db, project
):
    piece = _piece(
        db,
        project,
        ContentStatus.ARCHIVED,
        (Platform.DEVTO, PublicationStatus.CANCELLED),
        (Platform.MEDIUM, PublicationStatus.FAILED),
    )
    resp = _move(client, auth, piece, "patch", "approved")
    assert resp.status_code == 409
    _move(client, auth, piece, "patch", "draft")
    resp = _move(client, auth, piece, "patch", "approved")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_archiving_an_archived_piece_is_a_no_op(client, auth, db, archived_live):
    """Idempotent: the settle runs on every call, and must change nothing on
    the second."""
    before = [(r.status, r.scheduled_for) for r in _rows(db, archived_live)]
    resp = _move(client, auth, archived_live, "patch", "archived")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    assert [(r.status, r.scheduled_for) for r in _rows(db, archived_live)] == before


# --------------------------------------------------------------------------- #
# Arming approves                                                              #
# --------------------------------------------------------------------------- #


def test_retrying_a_row_on_a_draft_approves_the_draft(client, auth, db, project, connect):
    """Otherwise the retry re-creates the contradiction the demotion removed."""
    connect(Platform.DEVTO)
    piece = _piece(
        db, project, ContentStatus.DRAFT, (Platform.DEVTO, PublicationStatus.FAILED)
    )
    row = _rows(db, piece)[0]

    resp = client.post(f"{API}/{piece.id}/retry/{row.id}", headers=auth)

    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Content, piece.id).status in (
        ContentStatus.APPROVED,
        ContentStatus.PUBLISHED,
        ContentStatus.FAILED,
    )
    assert db.get(Content, piece.id).status != ContentStatus.DRAFT


def test_dragging_a_draft_onto_the_calendar_approves_it(client, auth, db, project):
    piece = _piece(
        db, project, ContentStatus.REVIEW, (Platform.DEVTO, PublicationStatus.CANCELLED)
    )
    resp = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": _future(days=4)},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.APPROVED
    assert _rows(db, piece)[0].status == PublicationStatus.SCHEDULED


def test_dating_a_draft_with_nothing_queued_does_not_approve_it(
    client, auth, db, project
):
    """A date on a draft is a plan, not an arming: nothing will act on it until
    somebody approves the piece, and ``release_approved`` keeps the date then."""
    piece = _piece(db, project, ContentStatus.DRAFT)
    resp = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": _future(days=4)},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Content, piece.id).status == ContentStatus.DRAFT


# --------------------------------------------------------------------------- #
# The worker's outcome survives an edit that landed during the platform call   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def queued(db, project, connect):
    connect(Platform.DEVTO)
    piece = _piece(db, project, ContentStatus.APPROVED)
    row = publishing_service.queue(db, piece, ["devto"])[0]
    db.commit()
    return row


def _stale_during_call(monkeypatch, outcome):
    """An adapter whose platform call coincides with a commit to the piece.

    ``set_committed_value`` is how a concurrent commit is staged in this suite:
    one shared SQLite connection, so no second session — what matters is the
    state left behind, an instance whose loaded version no longer matches the
    row's. See ``test_two_editors_cannot_silently_overwrite_each_other``.
    """

    def _publish(self, request, credentials):
        publication = self._probe
        set_committed_value(publication.content, "version", 41)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(DevToAdapter, "publish", _publish)


def test_a_success_is_recorded_against_the_row_as_it_now_stands(
    db, queued, monkeypatch
):
    DevToAdapter._probe = queued
    _stale_during_call(
        monkeypatch, PublishResult(external_id="1", external_url=LIVE_URL)
    )

    publishing_service.execute(db, queued)  # used to raise StaleDataError

    db.expire_all()
    row = db.get(Publication, queued.id)
    assert row.status == PublicationStatus.PUBLISHED
    assert row.external_url == LIVE_URL
    assert row.published_at is not None
    assert row.attempts == 1
    assert row.duration_ms is not None
    piece = db.get(Content, row.content_id)
    assert piece.status == ContentStatus.PUBLISHED
    assert piece.published_at is not None
    assert piece.canonical_url == LIVE_URL


def test_a_terminal_failure_is_recorded_the_same_way(db, queued, monkeypatch):
    DevToAdapter._probe = queued
    monkeypatch.setattr("app.config.settings.publish_max_retries", 1)
    _stale_during_call(monkeypatch, PublishError("platform said no"))

    publishing_service.execute(db, queued)

    db.expire_all()
    row = db.get(Publication, queued.id)
    assert row.status == PublicationStatus.FAILED
    assert row.attempts == 1
    assert "platform said no" in row.error
    assert db.get(Content, row.content_id).status == ContentStatus.FAILED


def test_a_row_that_keeps_moving_is_still_an_error(db, queued, monkeypatch):
    """Three tries, then the exception — a piece being rewritten in a tight
    loop is not something to paper over."""
    calls = 0

    def _always_stale(publication):
        nonlocal calls
        calls += 1
        set_committed_value(publication.content, "version", 41 + calls)
        publication.content.title = f"moved {calls}"

    with pytest.raises(StaleDataError):
        publishing_service._commit_outcome(db, queued, _always_stale)
    assert calls == 3
    db.rollback()


def test_an_undisturbed_commit_costs_nothing_extra(db, queued, monkeypatch, sql_log):
    """The common case is one commit; the retry only runs when it has to."""
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, request, credentials: PublishResult(
            external_id="1", external_url=LIVE_URL
        ),
    )
    publishing_service.execute(db, queued)
    assert sum("ROLLBACK" in s.upper() for s in sql_log) == 0
    db.expire_all()
    assert db.get(Publication, queued.id).status == PublicationStatus.PUBLISHED
