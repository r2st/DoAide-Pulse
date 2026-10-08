"""The content row and its publications must not answer the same question twice.

Two columns on ``Content`` are only ever true because something kept them true:
``status``, which is derived from the publications by
``publishing_service.sync_content_status``, and ``scheduled_for``, which is the
piece's own copy of when it is going out. Both have a set of writers that agree
about them, and both had one writer that did not.

* ``POST /content/{id}/retry/{publication_id}`` re-armed a publication on an
  *archived* piece. ``_queue_publish`` writes the invariant down — "an archived
  piece has nothing armed, ever" — and the calendar's reschedule keeps it; this
  path was the hole. Nothing could come of the arming, because
  ``publishing_service.execute`` cancels the row at the last gate, so the whole
  effect was a 200 on a piece that is not going anywhere and a row that read
  ``pending`` until a worker settled it back.

* ``PATCH /content/{id}`` wrote ``scheduled_for`` raw: no
  ``scheduling.normalize``, so a mistyped year was accepted here and refused
  with a 422 by the two endpoints that write the same column; and no pass over
  the publications, so a piece scheduled for Tuesday and PATCHed to Friday went
  out on Tuesday while the editor showed Friday.

The last test in the file is the general one: it walks the database looking for
rows that contradict each other, and it is the assertion the rest of these
flows are exercised against.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service

API = "/api/v1/content"

#: Statuses a worker may still act on. The complement of ``is_terminal``.
ARMED = (
    PublicationStatus.PENDING,
    PublicationStatus.SCHEDULED,
    PublicationStatus.PUBLISHING,
)


def _future(days: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@pytest.fixture
def piece(db, project) -> Content:
    """An approved piece, ready to be queued."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Shipping a scheduler",
        slug="shipping-a-scheduler",
        body_markdown="# Shipping\n\nA body long enough to look like a post.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _schedule(client, auth, piece, *platforms, when: str | None = None):
    resp = client.post(
        f"{API}/{piece.id}/schedule",
        json={
            "platforms": [p.value for p in platforms],
            "scheduled_for": when or _future(),
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Retrying an archived piece                                                   #
# --------------------------------------------------------------------------- #


def test_retrying_an_archived_piece_is_refused(client, auth, db, piece, connect):
    """The hole in "an archived piece has nothing armed, ever".

    Archiving cancels the armed rows and puts ``ARCHIVED_ERROR`` on them — a
    cancelled row with an error beside it, which is exactly the shape a Retry
    button offers to act on. So this was reachable by clicking Reject in the
    review queue and then Retry in the publications list, and Retry appeared to
    win: 200, ``pending``, and a piece the user had just withdrawn sitting back
    in the queue.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    assert client.patch(
        f"{API}/{piece.id}", json={"status": "archived"}, headers=auth
    ).status_code == 200

    publication = db.scalars(select(Publication)).one()
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.error == publishing_service.ARCHIVED_ERROR

    resp = client.post(f"{API}/{piece.id}/retry/{publication.id}", headers=auth)
    assert resp.status_code == 409, resp.text
    assert "archived" in resp.json()["detail"]

    db.expire_all()
    assert db.get(Publication, publication.id).status == PublicationStatus.CANCELLED


def test_the_refusal_is_the_one_the_other_arming_paths_give(
    client, auth, db, piece, connect
):
    """Same wording as ``POST /publish`` and the calendar's reschedule.

    Three endpoints can arm a publication and all three now say the same thing
    about an archived piece. A user who meets one of them and then another
    should not have to work out whether they are two different rules.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    publication = db.scalars(select(Publication)).one()
    client.patch(f"{API}/{piece.id}", json={"status": "archived"}, headers=auth)

    retry = client.post(f"{API}/{piece.id}/retry/{publication.id}", headers=auth)
    publish = client.post(
        f"{API}/{piece.id}/publish", json={"platforms": ["devto"]}, headers=auth
    )
    reschedule = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": _future()},
        headers=auth,
    )

    assert retry.status_code == publish.status_code == reschedule.status_code == 409
    assert (
        retry.json()["detail"]
        == publish.json()["detail"]
        == reschedule.json()["detail"]
    )


def test_un_archiving_makes_the_retry_available_again(client, auth, db, piece, connect):
    """The refusal is about the archive, not about the row.

    "Take it out of the archive first" has to be advice that works, or it is
    just a wall with a sentence on it.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    publication = db.scalars(select(Publication)).one()
    client.patch(f"{API}/{piece.id}", json={"status": "archived"}, headers=auth)
    client.patch(f"{API}/{piece.id}", json={"status": "draft"}, headers=auth)
    client.patch(f"{API}/{piece.id}", json={"status": "approved"}, headers=auth)

    resp = client.post(f"{API}/{piece.id}/retry/{publication.id}", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("pending", "published", "failed")


def test_a_failed_publication_on_a_live_piece_can_still_be_retried(
    client, auth, db, piece, connect
):
    """The guard reads the piece's status, and archived is the only one it stops.

    A piece that is ``published`` because one platform took it and ``failed`` on
    another is the ordinary reason to click Retry, and it must not be caught by
    a check aimed at withdrawn work.
    """
    connect(Platform.DEVTO, Platform.MEDIUM)
    _schedule(client, auth, piece, Platform.DEVTO, Platform.MEDIUM)
    devto, medium = (
        db.scalars(select(Publication).order_by(Publication.platform)).all()
    )
    devto.status = PublicationStatus.PUBLISHED
    devto.published_at = datetime.now(UTC)
    devto.scheduled_for = None
    medium.status = PublicationStatus.FAILED
    medium.scheduled_for = None
    medium.error = "the platform said no"
    publishing_service.sync_content_status(piece)
    db.commit()
    assert piece.status == ContentStatus.PUBLISHED

    resp = client.post(f"{API}/{piece.id}/retry/{medium.id}", headers=auth)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# PATCHing the piece's date                                                    #
# --------------------------------------------------------------------------- #


def test_patching_a_date_into_the_past_is_refused(client, auth, piece):
    """``POST /schedule`` answers 422 for this value; so does PATCH now.

    A mistyped year is the failure mode ``scheduling.normalize`` exists for, and
    it does not become a different mistake because it arrived on a different
    endpoint.
    """
    past = (datetime.now(UTC) - timedelta(days=400)).isoformat()

    patched = client.patch(f"{API}/{piece.id}", json={"scheduled_for": past}, headers=auth)
    scheduled = client.post(
        f"{API}/{piece.id}/schedule",
        json={"platforms": ["devto"], "scheduled_for": past},
        headers=auth,
    )

    assert patched.status_code == 422, patched.text
    assert scheduled.status_code == 422
    assert "in the past" in patched.json()["detail"]


def test_patching_a_date_beyond_the_horizon_is_refused(client, auth, piece):
    """The other half of ``normalize`` — a year typed one digit wide."""
    resp = client.patch(
        f"{API}/{piece.id}",
        json={"scheduled_for": (datetime.now(UTC) + timedelta(days=4000)).isoformat()},
        headers=auth,
    )
    assert resp.status_code == 422, resp.text
    assert "check the year" in resp.json()["detail"]


def test_a_naive_time_is_read_as_utc(client, auth, db, piece):
    """Same reading as every other writer of the column.

    Clients that hand-assemble ``"2026-08-04T09:00"`` are common enough that
    ``normalize`` accepts them rather than being pedantic; going through it here
    is what makes this endpoint agree.
    """
    naive = (datetime.now(UTC) + timedelta(days=3)).replace(tzinfo=None)
    resp = client.patch(
        f"{API}/{piece.id}",
        json={"scheduled_for": naive.isoformat()},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    stored = db.get(Content, piece.id).scheduled_for
    assert stored is not None
    assert abs(stored.replace(tzinfo=UTC) - naive.replace(tzinfo=UTC)) < timedelta(
        seconds=1
    )


def test_moving_the_piece_moves_what_is_still_queued(client, auth, db, piece, connect):
    """The divergence itself: two columns, one date, and the wrong one on screen.

    The piece was scheduled for Tuesday and PATCHed to Friday. The publication
    kept Tuesday and the beat sweep publishes from the publication, so the post
    went out on Tuesday while the editor — which reads ``content.scheduled_for``
    — showed Friday and gave nobody a reason to look.
    """
    connect(Platform.DEVTO)
    tuesday = _future(days=2)
    _schedule(client, auth, piece, Platform.DEVTO, when=tuesday)

    friday = _future(days=5)
    resp = client.patch(f"{API}/{piece.id}", json={"scheduled_for": friday}, headers=auth)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    content = db.get(Content, piece.id)
    publication = db.scalars(select(Publication)).one()
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for == content.scheduled_for
    assert abs(
        publication.scheduled_for.replace(tzinfo=UTC)
        - datetime.fromisoformat(friday)
    ) < timedelta(seconds=1)


def test_clearing_the_date_returns_the_queue_to_pending(
    client, auth, db, piece, connect
):
    """``null`` means "as soon as a worker picks it up", on both rows.

    ``scheduled`` with no ``scheduled_for`` is the contradiction the reschedule
    endpoint already avoids by flipping the status with the column; clearing the
    date here has to do the same or it leaves a row whose status refers to a
    time that is gone.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)

    resp = client.patch(f"{API}/{piece.id}", json={"scheduled_for": None}, headers=auth)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    publication = db.scalars(select(Publication)).one()
    assert publication.scheduled_for is None
    assert publication.status == PublicationStatus.PENDING
    assert db.get(Content, piece.id).scheduled_for is None


def test_a_published_row_keeps_the_time_it_actually_went_out(
    client, auth, db, piece, connect
):
    """A published row's date is history, not a plan.

    ``scheduled_for`` is exempt from the edit freeze on a live piece, so this
    PATCH is allowed — and it must not rewrite the record of when the post
    happened, or re-arm a row that is already on a platform.
    """
    connect(Platform.DEVTO, Platform.MEDIUM)
    _schedule(client, auth, piece, Platform.DEVTO, Platform.MEDIUM)
    devto, medium = db.scalars(select(Publication).order_by(Publication.platform)).all()
    went_out = datetime.now(UTC) - timedelta(hours=1)
    devto.status = PublicationStatus.PUBLISHED
    devto.published_at = went_out
    devto.scheduled_for = went_out
    devto.external_url = "https://dev.to/pulse/shipping-a-scheduler"
    publishing_service.sync_content_status(piece)
    db.commit()

    resp = client.patch(
        f"{API}/{piece.id}", json={"scheduled_for": _future(days=6)}, headers=auth
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert db.get(Publication, devto.id).scheduled_for.replace(
        tzinfo=UTC
    ) == went_out.replace(microsecond=went_out.microsecond)
    assert db.get(Publication, devto.id).status == PublicationStatus.PUBLISHED
    # The copy that has not gone yet does move.
    assert db.get(Publication, medium.id).status == PublicationStatus.SCHEDULED
    assert db.get(Publication, medium.id).scheduled_for == db.get(
        Content, piece.id
    ).scheduled_for


def test_a_claimed_row_is_not_pulled_back_out_of_a_workers_hands(
    client, auth, db, piece, connect
):
    """``publishing`` is the one status where re-timing means posting twice.

    The calendar's reschedule refuses a claimed row outright for this reason. A
    general-purpose PATCH answering 409 would be a new way for an ordinary edit
    to fail, so it leaves the row alone instead — the row is about to become
    ``published``, at which point its time is history like any other.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    publication = db.scalars(select(Publication)).one()
    claimed_for = publication.scheduled_for
    publication.status = PublicationStatus.PUBLISHING
    db.commit()

    resp = client.patch(
        f"{API}/{piece.id}", json={"scheduled_for": _future(days=9)}, headers=auth
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    publication = db.get(Publication, publication.id)
    assert publication.status == PublicationStatus.PUBLISHING
    assert publication.scheduled_for == claimed_for


def test_a_cancelled_row_is_not_re_opened_by_a_date(client, auth, db, piece, connect):
    """Cancelling is a settled question. Moving the piece does not re-ask it."""
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    assert client.delete(f"{API}/{piece.id}/schedule", headers=auth).status_code == 200

    resp = client.patch(
        f"{API}/{piece.id}", json={"scheduled_for": _future(days=4)}, headers=auth
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    publication = db.scalars(select(Publication)).one()
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.scheduled_for is None


def test_archiving_in_the_same_patch_as_a_date_still_clears_the_queue(
    client, auth, db, piece, connect
):
    """Archiving wins over the date it arrived with.

    Both branches run in one request, so the order matters: the date is carried
    onto the queue and then ``cancel_armed`` takes the queue away again. A piece
    that came out of this both archived and dated would be on the calendar under
    a status that means it is not going out.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)

    resp = client.patch(
        f"{API}/{piece.id}",
        json={"status": "archived", "scheduled_for": _future(days=3)},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    content = db.get(Content, piece.id)
    publication = db.scalars(select(Publication)).one()
    assert content.status == ContentStatus.ARCHIVED
    assert content.scheduled_for is None
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.scheduled_for is None


def test_a_piece_with_nothing_queued_can_still_be_dated(client, auth, db, piece):
    """The standalone case, which is why the field is not simply refused.

    A piece with no publications is drawn on the calendar from this column
    alone — "schedule this for Tuesday" before choosing where it goes — so the
    propagation above has to be a no-op rather than a requirement.
    """
    resp = client.patch(
        f"{API}/{piece.id}", json={"scheduled_for": _future(days=7)}, headers=auth
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    assert db.get(Content, piece.id).scheduled_for is not None
    assert db.scalars(select(Publication)).all() == []


# --------------------------------------------------------------------------- #
# The invariant itself                                                         #
# --------------------------------------------------------------------------- #


def contradictions(db) -> list[str]:
    """Every row pair in the database that disagrees with itself.

    Written as a scan rather than as an assertion per flow so that it can be
    pointed at whatever state a test has just produced. Each rule is one the
    tree states somewhere in prose; this is where they are all in one place.
    """
    found: list[str] = []
    db.expire_all()

    for content in db.scalars(select(Content)):
        publications = list(content.publications)
        live = [p for p in publications if p.status != PublicationStatus.CANCELLED]
        armed = [p.platform.value for p in publications if p.status in ARMED]

        went_out = any(p.status == PublicationStatus.PUBLISHED for p in publications)
        if content.status == ContentStatus.PUBLISHED:
            if not went_out:
                found.append(f"content {content.id} is published with nothing published")
            if content.published_at is None:
                found.append(f"content {content.id} is published with no published_at")
        # Not keyed to the status: archiving a piece that went out keeps both the
        # date and the publication, because archiving means "stop showing me
        # this" rather than "this never happened". The rule is that the date and
        # the queue agree, whatever the column says.
        if content.published_at is not None and not went_out:
            found.append(
                f"content {content.id} has a published_at with nothing published"
            )

        # Archived, and the two statuses before approval: nothing is queued
        # from any of them. A draft with a scheduled row is a draft the sweep
        # will publish — see ``routers.content._settle_status``.
        if content.status in (
            ContentStatus.DRAFT,
            ContentStatus.REVIEW,
            ContentStatus.ARCHIVED,
        ):
            if armed:
                found.append(
                    f"content {content.id} is {content.status.value} but armed on {armed}"
                )
            if content.status == ContentStatus.ARCHIVED and content.scheduled_for is not None:
                found.append(f"content {content.id} is archived but still dated")

        if content.status == ContentStatus.FAILED and (
            not live or not all(p.is_terminal for p in live)
        ):
            found.append(f"content {content.id} is failed with something still to try")

    for publication in db.scalars(select(Publication)):
        if publication.status == PublicationStatus.PUBLISHED:
            if publication.published_at is None:
                found.append(f"publication {publication.id} is published with no date")
        elif publication.status == PublicationStatus.SCHEDULED:
            if publication.scheduled_for is None:
                found.append(f"publication {publication.id} is scheduled for nothing")
        elif publication.is_terminal and publication.scheduled_for is not None:
            found.append(
                f"publication {publication.id} is {publication.status.value} "
                "but still dated"
            )
        if publication.status == PublicationStatus.FAILED and not publication.error:
            found.append(f"publication {publication.id} failed with no reason given")

    return found


def test_the_ordinary_lifecycle_leaves_nothing_contradictory(
    client, auth, db, piece, connect
):
    """Queue it, move it, publish part of it, archive the rest.

    One piece taken through the paths that write both columns, with the scan run
    after each step rather than only at the end — a state that is repaired by
    the *next* call is still a state the API returned and the UI drew.
    """
    connect(Platform.DEVTO, Platform.MEDIUM)
    assert contradictions(db) == []

    _schedule(client, auth, piece, Platform.DEVTO, Platform.MEDIUM)
    assert contradictions(db) == []

    client.patch(f"{API}/{piece.id}", json={"scheduled_for": _future(days=5)}, headers=auth)
    assert contradictions(db) == []

    devto = db.scalars(
        select(Publication).where(Publication.platform == Platform.DEVTO)
    ).one()
    devto.status = PublicationStatus.PUBLISHED
    devto.published_at = datetime.now(UTC)
    devto.scheduled_for = None
    devto.external_url = "https://dev.to/pulse/shipping-a-scheduler"
    publishing_service.sync_content_status(db.get(Content, piece.id))
    db.commit()
    assert contradictions(db) == []

    client.patch(f"{API}/{piece.id}", json={"status": "archived"}, headers=auth)
    assert contradictions(db) == []


def test_the_scan_would_have_caught_both_of_these(client, auth, db, piece, connect):
    """The scan is only worth having if it fails on the states that were real.

    Both bugs are re-created here by hand — the rows exactly as the two
    endpoints used to leave them — so that a future change which re-opens either
    path is caught by the scan rather than only by the endpoint tests above.
    """
    connect(Platform.DEVTO)
    _schedule(client, auth, piece, Platform.DEVTO)
    client.patch(f"{API}/{piece.id}", json={"status": "archived"}, headers=auth)
    assert contradictions(db) == []

    publication = db.scalars(select(Publication)).one()
    publication.status = PublicationStatus.PENDING
    publication.scheduled_for = None
    db.commit()
    assert contradictions(db) == ["content 1 is archived but armed on ['devto']"]

    publication.status = PublicationStatus.SCHEDULED
    db.commit()
    assert sorted(contradictions(db)) == [
        "content 1 is archived but armed on ['devto']",
        "publication 1 is scheduled for nothing",
    ]
