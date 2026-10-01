"""Archiving, bulk retry, and moving a piece between statuses.

Three endpoints added beside machinery that already had opinions about all
three, so most of what is asserted here is *agreement*: the new paths must
reach the same conclusions as the ones they sit next to, because a second way
to do something is a second thing that can be wrong.

The specific agreements:

* A retry re-arms through the same helper as the per-publication retry, so a
  syndicated copy is held behind its original rather than dispatched ahead of
  it — the failure that puts a post on a platform before there is a canonical
  URL for it to point at.
* Archiving cancels the armed publications, wherever it is done from. An
  archived piece with something still queued is a piece the beat sweep
  publishes after the user withdrew it.
* ``published`` and ``failed`` are derived from a piece's publications and are
  refused by the new endpoint exactly as ``PATCH`` refuses them.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    """Record dispatches instead of publishing.

    The retry endpoints hand re-armed rows to a worker, and with Celery off
    that runs the whole publish inline on the request thread — which would make
    these tests about the platform adapters.
    """
    from app.routers import content as content_router

    dispatched: list[int] = []
    monkeypatch.setattr(content_router, "_dispatch", dispatched.extend)
    return dispatched


@pytest.fixture
def dispatched(_no_dispatch):
    return _no_dispatch


#: A body, because a piece without one cannot be submitted for review.
#:
#: These rows used to be created with the empty default, which was fine while
#: every transition in this file was a column assignment. ``draft`` → ``review``
#: is not one any more: it runs the quality floor (see
#: ``app.routers.content._assert_review_ready``), and an empty body scores zero
#: — deliberately, because the two components that would otherwise carry it are
#: both asking "is there too much of the wrong thing" and neither can see that
#: there is nothing at all. One paragraph is all the gate wants, and the pieces
#: in this file are stand-ins for real ones, so giving them one costs nothing
#: and stops the fixture from being the only thing under test.
_BODY = (
    "Pulse writes a post from a repository's commits and puts it in front of "
    "a human before it goes out. This piece is a stand-in for one of those."
)


def _content(db, project, *, status=ContentStatus.DRAFT, title="A piece", age_days=0):
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{utcnow().timestamp()}",
        body_markdown=_BODY,
        status=status,
    )
    db.add(row)
    db.flush()
    if age_days:
        row.created_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return row


def _publication(db, content, *, platform=Platform.DEVTO, status, error=None):
    row = Publication(
        content_id=content.id, platform=platform, status=status, error=error
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# POST /content/{id}/retry                                                     #
# --------------------------------------------------------------------------- #


def test_a_piece_retries_all_of_its_failed_publications(
    client, auth, db, project, dispatched
):
    """The endpoint the publications list wants — a piece usually fails on more
    than one platform, and retrying one row at a time is the same call repeated.

    ``canonical_url`` is set so nothing is held behind a syndication original:
    this test is about the fan-out, and the hold has its own test below.
    """
    piece = _content(db, project)
    piece.canonical_url = "https://herald.example.com/a-piece"
    db.commit()
    a = _publication(db, piece, platform=Platform.DEVTO, status=PublicationStatus.FAILED)
    b = _publication(
        db, piece, platform=Platform.HASHNODE, status=PublicationStatus.FAILED
    )

    resp = client.post(f"/api/v1/content/{piece.id}/retry", headers=auth)

    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["retried"]) == sorted([a.id, b.id])
    db.expire_all()
    assert {p.status for p in db.query(Publication)} == {PublicationStatus.PENDING}
    assert sorted(dispatched) == sorted([a.id, b.id])


def test_a_retry_clears_the_error_and_the_attempt_count(client, auth, db, project):
    piece = _content(db, project)
    pub = _publication(
        db, piece, status=PublicationStatus.FAILED, error="platform said no"
    )
    pub.attempts = 3
    db.commit()

    client.post(f"/api/v1/content/{piece.id}/retry", headers=auth)

    db.refresh(pub)
    assert pub.error is None
    assert pub.attempts == 0


def test_a_published_row_is_skipped_not_retried(client, auth, db, project, dispatched):
    """Retrying it would post a second copy."""
    piece = _content(db, project)
    live = _publication(
        db, piece, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    failed = _publication(
        db, piece, platform=Platform.HASHNODE, status=PublicationStatus.FAILED
    )

    body = client.post(f"/api/v1/content/{piece.id}/retry", headers=auth).json()

    assert body["retried"] == [failed.id]
    assert [s["content_id"] for s in body["skipped"]] == [live.id]
    assert dispatched == [failed.id]
    db.refresh(live)
    assert live.status == PublicationStatus.PUBLISHED


def test_an_in_flight_row_is_left_alone(client, auth, db, project, dispatched):
    """A row a worker has claimed is about to succeed.

    Putting it back to pending is how a post goes out twice.
    """
    piece = _content(db, project)
    _publication(db, piece, status=PublicationStatus.PUBLISHING)

    body = client.post(f"/api/v1/content/{piece.id}/retry", headers=auth).json()

    assert body["retried"] == []
    assert dispatched == []


def test_a_piece_with_nothing_to_retry_answers_200_not_409(client, auth, db, project):
    """The caller named a piece, and "nothing needed retrying" is true about it."""
    piece = _content(db, project)
    _publication(db, piece, status=PublicationStatus.PUBLISHED)

    resp = client.post(f"/api/v1/content/{piece.id}/retry", headers=auth)

    assert resp.status_code == 200
    assert resp.json()["retried"] == []


def test_retrying_an_archived_piece_is_refused(client, auth, db, project):
    """Arming it cannot publish it — ``execute`` cancels the row at the last gate.

    So a 200 here would answer success to a caller whose piece is not going
    anywhere. The review queue's Reject and the publications list's Retry
    disagreed once before, and Retry appeared to win.
    """
    piece = _content(db, project, status=ContentStatus.ARCHIVED)
    _publication(db, piece, status=PublicationStatus.CANCELLED, error="archived")

    resp = client.post(f"/api/v1/content/{piece.id}/retry", headers=auth)

    assert resp.status_code == 409
    assert "archive" in resp.json()["detail"].lower()


def test_a_syndicated_copy_is_held_behind_its_original(
    client, auth, db, project, dispatched
):
    """The property :func:`_rearm` exists to keep in one place.

    A copy whose original has not published yet comes back ``scheduled`` for
    the beat sweep rather than being dispatched now — otherwise it lands on a
    platform before there is a canonical URL for it to point at.
    """
    from app.services import publishing_service

    piece = _content(db, project)
    original = _publication(
        db, piece, platform=Platform.DEVTO, status=PublicationStatus.FAILED
    )
    copy = _publication(
        db, piece, platform=Platform.HASHNODE, status=PublicationStatus.FAILED
    )
    db.refresh(piece)

    # A guard on the fixture rather than a skip: if this pair stops being
    # syndicated the assertions below would pass vacuously, and a test that
    # cannot fail is worse than one that is missing. Asked with the original
    # re-armed, because that is the state the endpoint computes the copy's
    # hold in: a *failed* original is terminal and holds nothing behind it,
    # and the endpoint re-arms the original before it reaches the copy.
    original.status = PublicationStatus.PENDING
    assert publishing_service.retry_hold(piece, copy) is not None, (
        "this test needs a syndicated pair to be meaningful; the canonical "
        "platform rules have changed and it should be re-derived"
    )
    original.status = PublicationStatus.FAILED
    db.commit()

    client.post(f"/api/v1/content/{piece.id}/retry", headers=auth)

    db.refresh(original)
    db.refresh(copy)
    # The original goes now; the copy waits for it, so it cannot land on a
    # platform before there is a canonical URL to point at.
    assert original.status == PublicationStatus.PENDING
    assert copy.status == PublicationStatus.SCHEDULED
    assert copy.scheduled_for is not None
    assert dispatched == [original.id], "a held copy must not be dispatched now"


def test_retrying_someone_elses_piece_is_a_404(client, auth, db, user):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="S",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    their_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        repo_url="https://github.com/r2st/Theirs",
        tone=Tone.TECHNICAL,
    )
    db.add(their_project)
    db.commit()
    piece = _content(db, their_project)

    assert client.post(f"/api/v1/content/{piece.id}/retry", headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# POST /content/bulk/retry                                                     #
# --------------------------------------------------------------------------- #


def test_a_bulk_retry_re_arms_every_named_piece(client, auth, db, project, dispatched):
    first = _content(db, project, title="one")
    second = _content(db, project, title="two")
    a = _publication(db, first, status=PublicationStatus.FAILED)
    b = _publication(db, second, status=PublicationStatus.FAILED)

    body = client.post(
        "/api/v1/content/bulk/retry",
        headers=auth,
        json={"content_ids": [first.id, second.id]},
    ).json()

    assert sorted(body["succeeded"]) == sorted([first.id, second.id])
    assert sorted(dispatched) == sorted([a.id, b.id])


def test_a_piece_with_nothing_retryable_is_reported_as_failed(client, auth, db, project):
    """A caller told "succeeded" for a live piece has been told its retry happened."""
    live = _content(db, project, title="live")
    _publication(db, live, status=PublicationStatus.PUBLISHED)

    body = client.post(
        "/api/v1/content/bulk/retry", headers=auth, json={"content_ids": [live.id]}
    ).json()

    assert body["succeeded"] == []
    assert body["failed"] == [{"content_id": live.id, "reason": "Nothing to retry"}]


def test_one_bad_piece_does_not_sink_the_batch(client, auth, db, project, dispatched):
    good = _content(db, project, title="good")
    pub = _publication(db, good, status=PublicationStatus.FAILED)
    archived = _content(db, project, title="archived", status=ContentStatus.ARCHIVED)

    body = client.post(
        "/api/v1/content/bulk/retry",
        headers=auth,
        json={"content_ids": [good.id, archived.id, 999999]},
    ).json()

    assert body["succeeded"] == [good.id]
    reasons = {f["content_id"]: f["reason"] for f in body["failed"]}
    assert reasons[999999] == "Not found"
    assert "archive" in reasons[archived.id].lower()
    assert dispatched == [pub.id]


# --------------------------------------------------------------------------- #
# POST /content/bulk/archive-old                                               #
# --------------------------------------------------------------------------- #


def test_old_drafts_are_archived_and_recent_ones_are_not(client, auth, db, project):
    old = _content(db, project, title="old", age_days=90)
    recent = _content(db, project, title="recent", age_days=2)

    body = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30},
    ).json()

    assert body["archived"] == [old.id]
    assert body["count"] == 1
    db.refresh(old)
    db.refresh(recent)
    assert old.status == ContentStatus.ARCHIVED
    assert recent.status == ContentStatus.DRAFT


def test_a_dry_run_changes_nothing(client, auth, db, project):
    """The one endpoint here that writes to rows the caller has not named."""
    old = _content(db, project, title="old", age_days=90)

    body = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30, "dry_run": True},
    ).json()

    assert body["archived"] == [old.id]
    assert body["dry_run"] is True
    db.refresh(old)
    assert old.status == ContentStatus.DRAFT


def test_a_published_piece_is_never_swept(client, auth, db, project):
    """Whatever the status list asks for.

    Archiving a live post is a deliberate act with consequences — it cancels
    the armed rows and hides the piece — and doing it to a month of live posts
    because somebody typed 30 is an outage in the analytics, not a convenience.
    """
    live = _content(db, project, title="live", status=ContentStatus.PUBLISHED, age_days=90)

    body = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30, "statuses": ["published", "draft"]},
    ).json()

    assert live.id not in body["archived"]
    db.refresh(live)
    assert live.status == ContentStatus.PUBLISHED


def test_asking_only_for_unsweepable_statuses_is_a_400(client, auth, project):
    """An empty IN would answer a cheerful zero that reads as "nothing was old"."""
    resp = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30, "statuses": ["published"]},
    )

    assert resp.status_code == 400


def test_the_sweep_cancels_what_was_armed(client, auth, db, project):
    """An archived piece with something queued is one the beat sweep publishes."""
    old = _content(db, project, title="old", status=ContentStatus.APPROVED, age_days=90)
    armed = _publication(db, old, status=PublicationStatus.SCHEDULED)

    client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30, "statuses": ["approved"]},
    )

    db.refresh(armed)
    assert armed.status == PublicationStatus.CANCELLED


def test_the_sweep_can_be_limited_to_one_project(client, auth, db, project, user):
    from app.models.project import Project, Tone

    other = Project(
        user_id=user.id,
        name="Other",
        slug="other",
        repo_url="https://github.com/r2st/Other",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.commit()
    mine = _content(db, project, title="mine", age_days=90)
    theirs = _content(db, other, title="theirs", age_days=90)

    body = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30, "project_id": project.id},
    ).json()

    assert body["archived"] == [mine.id]
    db.refresh(theirs)
    assert theirs.status == ContentStatus.DRAFT


def test_another_accounts_old_drafts_are_not_swept(client, auth, db):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="S",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    their_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        repo_url="https://github.com/r2st/Theirs",
        tone=Tone.TECHNICAL,
    )
    db.add(their_project)
    db.commit()
    theirs = _content(db, their_project, title="theirs", age_days=90)

    body = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": 30},
    ).json()

    assert body["archived"] == []
    db.refresh(theirs)
    assert theirs.status == ContentStatus.DRAFT


@pytest.mark.parametrize("days", [0, -1, 4000])
def test_an_out_of_range_age_is_refused(client, auth, project, days):
    resp = client.post(
        "/api/v1/content/bulk/archive-old",
        headers=auth,
        json={"older_than_days": days},
    )
    assert resp.status_code == 422


def test_the_age_has_no_default(client, auth, project):
    """The value that gets typed by accident is the one that was already there."""
    resp = client.post("/api/v1/content/bulk/archive-old", headers=auth, json={})

    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# POST /content/{id}/status                                                    #
# --------------------------------------------------------------------------- #


def test_a_piece_can_be_promoted(client, auth, db, project):
    piece = _content(db, project, status=ContentStatus.DRAFT)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"
    db.refresh(piece)
    assert piece.status == ContentStatus.REVIEW


@pytest.mark.parametrize("derived", ["published", "failed"])
def test_a_derived_status_is_refused(client, auth, db, project, derived):
    """These follow from the piece's publications.

    Setting either by hand makes the content row disagree with what is actually
    live — a piece counted as published in the analytics with nothing behind
    it. Refused by ``PATCH`` for the same reason, through the same validator.
    """
    piece = _content(db, project)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": derived}
    )

    assert resp.status_code == 422


def test_a_live_piece_may_only_be_archived(client, auth, db, project):
    """Moving it back to draft says it is not published while it still is."""
    piece = _content(db, project, status=ContentStatus.PUBLISHED)

    refused = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "draft"}
    )
    assert refused.status_code == 409

    allowed = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "archived"}
    )
    assert allowed.status_code == 200


def test_archiving_through_the_status_endpoint_cancels_the_queue(
    client, auth, db, project
):
    """The same pairing the PATCH and ``/bulk/reject`` both make."""
    piece = _content(db, project, status=ContentStatus.APPROVED)
    armed = _publication(db, piece, status=PublicationStatus.SCHEDULED)

    client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "archived"}
    )

    db.refresh(armed)
    assert armed.status == PublicationStatus.CANCELLED


def test_approving_through_the_status_endpoint_releases_the_piece(
    client, auth, db, project, connect, monkeypatch
):
    """Approving here must mean what the Approve button means.

    Asserted by watching ``release_approved`` rather than its side effects, so
    the test does not depend on which platforms happen to be connected.
    """
    from app.routers import content as content_router

    released: list[int] = []
    monkeypatch.setattr(
        content_router.content_pipeline,
        "release_approved",
        lambda db_, content: released.append(content.id),
    )
    piece = _content(db, project, status=ContentStatus.REVIEW)

    client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "approved"}
    )

    assert released == [piece.id]


def test_the_status_endpoint_returns_an_etag(client, auth, db, project):
    """So the next conditional save can carry it."""
    piece = _content(db, project)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.headers.get("ETag")


def test_setting_the_status_of_someone_elses_piece_is_a_404(client, auth, db):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="S",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    their_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        repo_url="https://github.com/r2st/Theirs",
        tone=Tone.TECHNICAL,
    )
    db.add(their_project)
    db.commit()
    piece = _content(db, their_project)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )
    assert resp.status_code == 404


def test_the_status_endpoint_and_the_patch_agree(client, auth, db, project):
    """Two doors to one column. If they ever disagree, one of them is a bug."""
    through_patch = _content(db, project, title="patched", status=ContentStatus.PUBLISHED)
    through_post = _content(db, project, title="posted", status=ContentStatus.PUBLISHED)

    patched = client.patch(
        f"/api/v1/content/{through_patch.id}", headers=auth, json={"status": "draft"}
    )
    posted = client.post(
        f"/api/v1/content/{through_post.id}/status",
        headers=auth,
        json={"status": "draft"},
    )

    assert patched.status_code == posted.status_code == 409
