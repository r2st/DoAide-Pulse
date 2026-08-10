"""Approving a piece is what releases it on an autopilot project.

The bug these pin down: five ``approved`` pieces sat on the production box for
days without publishing. Approving set ``Content.status`` and stopped — nothing
queued a publication, and the beat sweep only ever looks at the publications
table, so a piece with no rows there is invisible to it however long it waits.

Two halves are covered here. The endpoints release on the way through, and a
beat task sweeps up anything approved another way (including the rows that were
already stuck before the endpoints learned to).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware
from app.models.project import AutopilotMode
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import content_pipeline
from app.tasks import publish_tasks


@pytest.fixture
def auto_project(db, project):
    """A project configured exactly as the ones on the box are."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto", "bluesky"]
    db.commit()
    return project


def _content(db, project, *, status=ContentStatus.REVIEW, slug="herald-1-0", **kwargs):
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="Herald 1.0",
        slug=slug,
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
        status=status,
        **kwargs,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    """Record dispatches instead of publishing for real."""
    dispatched: list[int] = []
    monkeypatch.setattr(
        content_pipeline, "publish_now", lambda pid: dispatched.append(pid)
    )
    return dispatched


# --------------------------------------------------------------------------- #
# The service                                                                  #
# --------------------------------------------------------------------------- #


def test_approving_queues_the_projects_autopilot_destinations(db, auto_project):
    content = _content(db, auto_project, status=ContentStatus.APPROVED)

    publications = content_pipeline.release_approved(db, content)

    assert {p.platform for p in publications} == {Platform.DEVTO, Platform.BLUESKY}
    assert db.query(Publication).count() == 2


def test_the_original_is_dispatched_and_the_copy_waits(db, auto_project, _no_dispatch):
    """Only the article destination goes out now — the social copy is parked."""
    content = _content(db, auto_project, status=ContentStatus.APPROVED)

    publications = content_pipeline.release_approved(db, content)
    by_platform = {p.platform: p for p in publications}

    assert by_platform[Platform.DEVTO].scheduled_for is None
    assert by_platform[Platform.BLUESKY].status == PublicationStatus.SCHEDULED
    assert _no_dispatch == [by_platform[Platform.DEVTO].id]


def test_a_piece_still_in_review_is_not_released(db, auto_project):
    content = _content(db, auto_project, status=ContentStatus.REVIEW)
    assert content_pipeline.release_approved(db, content) == []
    assert db.query(Publication).count() == 0


def test_a_draft_is_not_released(db, auto_project):
    content = _content(db, auto_project, status=ContentStatus.DRAFT)
    assert content_pipeline.release_approved(db, content) == []


@pytest.mark.parametrize("mode", [AutopilotMode.OFF, AutopilotMode.DRAFT])
def test_a_project_that_does_not_publish_on_its_own_is_left_alone(db, project, mode):
    """``off`` and ``draft`` mean "not without me". Approving is not a destination."""
    project.autopilot_mode = mode
    project.autopilot_platforms = ["devto"]
    db.commit()
    content = _content(db, project, status=ContentStatus.APPROVED)

    assert content_pipeline.release_approved(db, content) == []
    assert db.query(Publication).count() == 0


def test_a_project_with_no_destinations_is_left_alone(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = []
    db.commit()
    content = _content(db, project, status=ContentStatus.APPROVED)

    assert content_pipeline.release_approved(db, content) == []


def test_an_inactive_project_is_left_alone(db, auto_project):
    auto_project.is_active = False
    db.commit()
    content = _content(db, auto_project, status=ContentStatus.APPROVED)

    assert content_pipeline.release_approved(db, content) == []


def test_a_piece_that_already_has_a_publication_is_not_added_to(db, auto_project):
    """A row of any status means a destination was already chosen for this."""
    content = _content(db, auto_project, status=ContentStatus.APPROVED)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.MEDIUM,
            status=PublicationStatus.CANCELLED,
        )
    )
    db.commit()
    db.refresh(content)

    assert content_pipeline.release_approved(db, content) == []
    assert db.query(Publication).count() == 1


def test_a_future_schedule_survives_the_release(db, auto_project, _no_dispatch):
    """Approving says yes to the piece, not "publish it now"."""
    when = datetime.now(UTC) + timedelta(days=3)
    content = _content(db, auto_project, status=ContentStatus.APPROVED, scheduled_for=when)

    publications = content_pipeline.release_approved(db, content)

    assert all(p.status == PublicationStatus.SCHEDULED for p in publications)
    # SQLite hands back a naive value for a column the app writes as aware.
    assert all(as_aware(p.scheduled_for) >= when for p in publications)
    assert _no_dispatch == []


def test_a_schedule_in_the_past_publishes_now(db, auto_project, _no_dispatch):
    when = datetime.now(UTC) - timedelta(days=3)
    content = _content(db, auto_project, status=ContentStatus.APPROVED, scheduled_for=when)

    publications = content_pipeline.release_approved(db, content)
    by_platform = {p.platform: p for p in publications}

    assert by_platform[Platform.DEVTO].scheduled_for is None
    assert _no_dispatch == [by_platform[Platform.DEVTO].id]


def test_an_unpublishable_destination_is_skipped(db, project):
    """A legacy row can name a platform with no finished adapter."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto", "linkedin"]
    db.commit()
    content = _content(db, project, status=ContentStatus.APPROVED)

    publications = content_pipeline.release_approved(db, content)

    assert [p.platform for p in publications] == [Platform.DEVTO]


def test_an_unknown_destination_does_not_raise(db, project):
    """A JSON column can hold anything; a beat sweep must survive it."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto", "myspace"]
    db.commit()
    content = _content(db, project, status=ContentStatus.APPROVED)

    publications = content_pipeline.release_approved(db, content)

    assert [p.platform for p in publications] == [Platform.DEVTO]


def test_an_upper_case_destination_is_understood(db, project):
    """Rows written through the ORM's enum machinery spell the name, not the value."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["DEVTO"]
    db.commit()
    content = _content(db, project, status=ContentStatus.APPROVED)

    publications = content_pipeline.release_approved(db, content)

    assert [p.platform for p in publications] == [Platform.DEVTO]


# --------------------------------------------------------------------------- #
# The endpoints                                                                #
# --------------------------------------------------------------------------- #


def test_the_approve_endpoint_releases(client, auth, db, auto_project):
    content = _content(db, auto_project)

    resp = client.post(f"/api/v1/content/{content.id}/approve", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert db.query(Publication).count() == 2


def test_the_bulk_approve_endpoint_releases(client, auth, db, auto_project):
    first = _content(db, auto_project, slug="one")
    second = _content(db, auto_project, slug="two")

    resp = client.post(
        "/api/v1/content/bulk/approve",
        headers=auth,
        json={"content_ids": [first.id, second.id]},
    )

    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["succeeded"]) == sorted([first.id, second.id])
    assert db.query(Publication).count() == 4


def test_approving_on_a_manual_project_still_only_changes_the_status(
    client, auth, db, project
):
    project.autopilot_mode = AutopilotMode.OFF
    db.commit()
    content = _content(db, project)

    resp = client.post(f"/api/v1/content/{content.id}/approve", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert db.query(Publication).count() == 0


def test_approving_does_not_reach_another_users_content(client, auth, db, auto_project):
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    auto_project.user_id = other.id
    db.commit()
    content = _content(db, auto_project)

    resp = client.post(f"/api/v1/content/{content.id}/approve", headers=auth)

    assert resp.status_code == 404
    assert db.query(Publication).count() == 0


# --------------------------------------------------------------------------- #
# The backstop sweep                                                           #
# --------------------------------------------------------------------------- #


def test_the_sweep_releases_a_piece_approved_before_the_fix(db, auto_project, monkeypatch):
    """The five rows already stuck on the box heal themselves on the next pass."""
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    _content(db, auto_project, status=ContentStatus.APPROVED)

    result = publish_tasks.release_approved_content()

    assert result == {"found": 1, "released": 1}
    assert db.query(Publication).count() == 2


def test_the_sweep_ignores_a_piece_that_is_already_queued(db, auto_project, monkeypatch):
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    content = _content(db, auto_project, status=ContentStatus.APPROVED)
    db.add(Publication(content_id=content.id, platform=Platform.DEVTO))
    db.commit()

    assert publish_tasks.release_approved_content() == {"found": 0, "released": 0}
    assert db.query(Publication).count() == 1


def test_the_sweep_ignores_projects_that_do_not_publish_on_their_own(
    db, project, monkeypatch
):
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    project.autopilot_mode = AutopilotMode.DRAFT
    project.autopilot_platforms = ["devto"]
    db.commit()
    _content(db, project, status=ContentStatus.APPROVED)

    assert publish_tasks.release_approved_content() == {"found": 0, "released": 0}


def test_the_sweep_leaves_review_and_published_alone(db, auto_project, monkeypatch):
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    _content(db, auto_project, status=ContentStatus.REVIEW, slug="a")
    _content(db, auto_project, status=ContentStatus.PUBLISHED, slug="b")

    assert publish_tasks.release_approved_content() == {"found": 0, "released": 0}
    assert db.query(Publication).count() == 0


def test_one_unreleasable_piece_does_not_stop_the_sweep(db, auto_project, monkeypatch):
    """A piece that blows up keeps its status; the rest of the sweep runs."""
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    first = _content(db, auto_project, status=ContentStatus.APPROVED, slug="a")
    _content(db, auto_project, status=ContentStatus.APPROVED, slug="b")

    real = content_pipeline.release_approved

    def _explode(session, content):
        if content.id == first.id:
            raise RuntimeError("boom")
        return real(session, content)

    monkeypatch.setattr(content_pipeline, "release_approved", _explode)

    result = publish_tasks.release_approved_content()

    assert result == {"found": 2, "released": 1}
    assert db.query(Publication).count() == 2


def test_the_sweep_stops_when_it_runs_out_of_time(db, auto_project, monkeypatch):
    """The soft limit ends the pass; it is not one piece's bad day.

    ``SoftTimeLimitExceeded`` is an ordinary ``Exception``, so the per-piece
    handler caught the timeout, logged it against whichever piece happened to
    be in hand, and carried on to the next — which is exactly what the soft
    limit exists to prevent. The sweep then ran until the *hard* limit killed
    the worker, with a transaction open. The remainder is not lost: nothing
    about a piece changes by being skipped, so the next pass finds it again.
    """
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    _content(db, auto_project, status=ContentStatus.APPROVED, slug="a")
    _content(db, auto_project, status=ContentStatus.APPROVED, slug="b")

    seen: list[int] = []

    def _timeout(session, content):
        seen.append(content.id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(content_pipeline, "release_approved", _timeout)

    result = publish_tasks.release_approved_content()

    assert result == {"found": 2, "released": 0}
    # The point of the fix: the second piece is never attempted.
    assert len(seen) == 1
    assert db.query(Publication).count() == 0


def test_a_database_failure_leaves_the_sweep_to_celery(db, monkeypatch):
    """A dead connection has to reach ``autoretry_for``, not become a result.

    The task builds its counts inside ``try``/``finally`` with no ``except``,
    and reports them after the block. That reads like it could return with
    ``stuck`` unbound, but it cannot: with nothing catching it the error
    propagates past the return entirely, which is what puts the sweep in
    celery's hands. Pinned because the alternative — swallowing it — would
    turn a broken database into a cheerful ``{"found": 0}`` every five minutes.
    """
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    closed: list[bool] = []
    monkeypatch.setattr(db, "close", lambda: closed.append(True))

    def _boom(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(db, "scalars", _boom)

    with pytest.raises(OperationalError):
        publish_tasks.release_approved_content()

    # OperationalError is in the task's autoretry_for, so this becomes a retry.
    assert OperationalError in publish_tasks.release_approved_content.autoretry_for
    # And the session still went back, which is what the `finally` is for.
    assert closed == [True]
