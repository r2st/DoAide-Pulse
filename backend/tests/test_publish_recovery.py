"""A publication that failed for want of a connection recovers when one arrives.

The ten pieces sitting in ``failed`` on production were all the same shape: an
account with no connections, an autopilot that armed the destinations anyway,
and ``NotConnected`` ending every row on the first attempt. The arming half is
fixed (``content_pipeline.publishable_destinations``). This is the other half —
the rows that fix left behind, and the ones any future account will leave behind
between connecting a platform and Pulse noticing.

What these pin, in order: that the recovery fires on the pair (right sentence,
live connection) and not on either half alone; that it leaves alone the failures
whose cure is *not* a connection, which is every other terminal failure there
is; and that a recovered row is armed the way a hand-retried one is, syndication
hold included.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publish_recovery, publishing_service


def _content(db, project, *, status=ContentStatus.FAILED, slug="a-piece") -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=status,
        title="A piece",
        slug=slug,
        body_markdown="# A piece\n\nSome words about the thing." * 20,
        excerpt="Some words.",
        meta_description="Some words about the thing.",
        keywords=["a"],
        tags=["a"],
        source={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _failed(db, content, platform: Platform, *, error: str | None = None) -> Publication:
    """A terminally failed publication, exactly as ``_fail(terminal=True)`` leaves it."""
    row = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.FAILED,
        attempts=1,
        error=(
            publishing_service.not_connected_error(platform) if error is None else error
        ),
        scheduled_for=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_row_that_only_wanted_a_connection_is_re_armed(db, project, connect):
    """The production case: connect the platform, and the piece comes back."""
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    ready = publish_recovery.recover(db)

    assert ready == [publication.id]
    db.refresh(publication)
    db.refresh(content)
    assert publication.status == PublicationStatus.PENDING
    # Reset, not carried: the attempt was spent on a question with a different
    # answer now, and counting it would start the row one failure from terminal.
    assert publication.attempts == 0
    assert publication.error is None
    # The piece is no longer out of platforms to try, and must not go on
    # claiming it is while a worker publishes it.
    assert content.status == ContentStatus.APPROVED


def test_nothing_is_recovered_while_the_platform_is_still_unconnected(db, project):
    """The half that is not evidence: the sentence, with no connection behind it.

    This is every one of the ten production rows as they stand today — the seed
    account has no connections at all — and the sweep running every few minutes
    must leave them exactly where they are rather than replaying them.
    """
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    db.refresh(content)
    assert publication.status == PublicationStatus.FAILED
    assert publication.attempts == 1
    assert content.status == ContentStatus.FAILED


def test_a_connection_for_another_platform_does_not_recover_this_one(db, project, connect):
    """The other half that is not evidence: a live connection, wrong platform."""
    content = _content(db, project)
    publication = _failed(db, content, Platform.BLUESKY)
    connect(Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_a_platform_that_refused_the_content_is_left_alone(db, project, connect):
    """Only the one failure whose cure is recorded elsewhere.

    A row the platform rejected on the piece's merits carries a different
    sentence, and re-arming it would replay the same refusal once per sweep
    forever. The connection being live is what makes this the dangerous case:
    every column except ``error`` looks like a recoverable row.
    """
    content = _content(db, project)
    publication = _failed(
        db, content, Platform.DEVTO, error="Dev.to rejected the post: title is taken"
    )
    connect(Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    assert publication.error == "Dev.to rejected the post: title is taken"


def test_a_rejected_credential_is_left_alone(db, project, connect):
    """The near-miss: a credential failure on a connection that is live again.

    ``CredentialError`` marks the connection ``invalid``; reconnecting sets it
    back to ``connected``, which is the exact state this sweep looks for. What
    keeps the row out is that it says something different — so the match has to
    be on the sentence and not merely on "terminal, and now connected".
    """
    content = _content(db, project)
    publication = _failed(
        db, content, Platform.DEVTO, error="Dev.to rejected the stored API key."
    )
    connect(Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_the_sentence_must_match_the_row_s_own_platform(db, project, connect):
    """A Bluesky row carrying the Dev.to sentence is a row nothing wrote."""
    content = _content(db, project)
    publication = _failed(
        db,
        content,
        Platform.BLUESKY,
        error=publishing_service.not_connected_error(Platform.DEVTO),
    )
    connect(Platform.BLUESKY, Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_an_archived_piece_is_not_dragged_back_out(db, project, connect):
    """Withdrawn is withdrawn. ``execute`` would cancel the row at its last gate,
    which is a state change nobody asked for on a piece somebody archived —
    and it would cost a dispatch to make it."""
    content = _content(db, project, status=ContentStatus.ARCHIVED)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


@pytest.mark.parametrize("status", [ContentStatus.DRAFT, ContentStatus.REVIEW])
def test_a_piece_taken_back_to_draft_is_not_dragged_back_out(
    db, project, connect, status
):
    """Demoting cancels the queue (``routers.content._settle_status``); a row
    this sweep armed on a draft would put it straight back, with nobody having
    said yes."""
    content = _content(db, project, status=status)
    row = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    assert publish_recovery.recover(db) == []
    db.refresh(row)
    assert row.status == PublicationStatus.FAILED
    db.refresh(content)
    assert content.status == status


def test_a_deactivated_account_recovers_nothing(db, project, user, connect):
    """The gate ``execute`` keeps for armed rows, kept here before arming."""
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)
    user.is_active = False
    db.commit()

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_an_inactive_project_recovers_nothing(db, project, connect):
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)
    project.is_active = False
    db.commit()

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_an_invalid_connection_is_not_a_live_one(db, project, connect):
    """``connected`` is the state that un-blocks. ``invalid`` is the state a
    rejected token leaves behind, and recovering into it would fail the row
    again on the next attempt for a reason the user has not fixed yet."""
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)
    connection = db.query(PlatformConnection).one()
    connection.status = ConnectionStatus.INVALID
    db.commit()

    assert publish_recovery.recover(db) == []
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_a_second_pass_does_not_see_what_the_first_recovered(db, project, connect):
    """Idempotent on a timer: the sweep runs every few minutes forever."""
    content = _content(db, project)
    _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    assert len(publish_recovery.recover(db)) == 1
    assert publish_recovery.recover(db) == []


def test_a_syndicated_copy_waits_behind_its_original(db, project, connect, monkeypatch):
    """Recovered the way a hand-retry is recovered, hold included.

    ``retry_publication`` goes through ``retry_hold`` because a copy published
    before its original has a canonical URL to point at is a mistake with no
    undo. A copy recovered by a sweep is the same copy.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "syndication_delay_seconds", 900)
    content = _content(db, project)
    # The original: still to go out, so the copy has something to wait for.
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            attempts=0,
            scheduled_for=utcnow() + timedelta(hours=1),
        )
    )
    db.commit()
    copy = _failed(db, content, Platform.BLUESKY)
    connect(Platform.BLUESKY)

    ready = publish_recovery.recover(db)

    db.refresh(copy)
    # Held, so not dispatched: its one route to a platform is the beat sweep,
    # which is where its time is checked.
    assert ready == []
    assert copy.status == PublicationStatus.SCHEDULED
    assert copy.scheduled_for is not None


def test_the_batch_is_bounded(db, project, connect):
    """A month's backlog must not become one tick's dispatch storm."""
    connect(Platform.DEVTO)
    for index in range(5):
        content = _content(db, project, slug=f"piece-{index}")
        _failed(db, content, Platform.DEVTO)

    assert len(publish_recovery.recover(db, limit=2)) == 2
    assert len(publish_recovery.recover(db, limit=2)) == 2
    assert len(publish_recovery.recover(db, limit=2)) == 1


def test_recoverable_reports_without_writing(db, project, connect):
    """The read half is a read: an operator can ask what would move."""
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    found = publish_recovery.recoverable(db)

    assert [p.id for p in found] == [publication.id]
    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED


def test_the_beat_task_re_arms_and_dispatches(db, project, connect, task_session, monkeypatch):
    """The wrapper: a session of its own, and the dispatch the service leaves to it."""
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)
    sent: list[int] = []
    monkeypatch.setattr(
        publish_tasks.content_pipeline, "publish_now", lambda pid: sent.append(pid)
    )
    content = _content(db, project)
    publication = _failed(db, content, Platform.DEVTO)
    connect(Platform.DEVTO)

    assert publish_tasks.recover_unblocked_publications() == {
        "recovered": 1,
        "dispatched": 1,
    }
    assert sent == [publication.id]


def test_the_beat_task_is_quiet_when_there_is_nothing_to_do(
    db, project, task_session, monkeypatch
):
    """The ordinary case, every few minutes forever: no rows, no dispatch, no noise."""
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)
    monkeypatch.setattr(
        publish_tasks.content_pipeline,
        "publish_now",
        lambda pid: pytest.fail("nothing should have been dispatched"),
    )
    _failed(db, _content(db, project), Platform.DEVTO)

    assert publish_tasks.recover_unblocked_publications() == {
        "recovered": 0,
        "dispatched": 0,
    }


def test_a_broker_that_refuses_one_id_does_not_cost_the_rest_their_dispatch(
    db, project, connect, task_session, monkeypatch
):
    """The rows are committed before any dispatch, so a refused hand-off is a
    delay of one sweep interval rather than a lost recovery."""
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)
    connect(Platform.DEVTO)
    for index in range(3):
        _failed(db, _content(db, project, slug=f"piece-{index}"), Platform.DEVTO)

    sent: list[int] = []
    seen: list[int] = []

    def _flaky(publication_id: int) -> None:
        seen.append(publication_id)
        if len(seen) == 2:  # the middle one, so there is a "rest" on both sides
            raise RuntimeError("broker down")
        sent.append(publication_id)

    monkeypatch.setattr(publish_tasks.content_pipeline, "publish_now", _flaky)

    result = publish_tasks.recover_unblocked_publications()

    assert result == {"recovered": 3, "dispatched": 2}
    # Every row is armed and due regardless of who took the dispatch — the
    # publish sweep picks up what the broker refused.
    assert (
        db.query(Publication)
        .filter(Publication.status == PublicationStatus.PENDING)
        .count()
        == 3
    )


@pytest.mark.parametrize("platform", [Platform.DEVTO, Platform.BLUESKY, Platform.GIT])
def test_the_sentence_is_the_one_the_rows_actually_carry(platform):
    """Pins the wire format of the marker the sweep matches on.

    The ten rows on production were written by an older build and say exactly
    this. Changing the wording without changing the matcher would strand them
    silently — the sweep would run, find nothing, and report success.
    """
    assert (
        publishing_service.not_connected_error(platform)
        == f"No live {platform.value} connection — add one in Settings."
    )
