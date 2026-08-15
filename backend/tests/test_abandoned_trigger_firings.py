"""Firings whose worker never came back.

``triggers.record`` commits the event at ``received`` before generation starts,
and that commit is what makes the dedupe key a promise: from that instant the
feed entry, webhook delivery or commit range can never start a second piece.
``fire`` is then the only thing in Herald that ever moves the row off
``received``.

So a process killed in between — OOM, a deploy restarting the worker,
``check_trigger``'s hard time limit, an API worker recycled mid-request on the
inbound webhook path — left a row nothing would ever settle, about news nothing
could ever re-deliver. It read "Fired." in the UI indefinitely, the retention
sweep deliberately kept it, and the piece it may well have written was
unreachable from it.

Two shapes, and the difference is a fact rather than a guess, because the piece
carries the firing's id in its ``source``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.services import triggers
from app.services.signals import TriggerSignal
from app.tasks import trigger_tasks


@pytest.fixture
def writing_project(db, project):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    return project


@pytest.fixture
def trigger(db, writing_project) -> Trigger:
    row = Trigger(
        project_id=writing_project.id,
        kind=TriggerKind.RSS,
        name="Changelog",
        config={"feed_url": "https://example.com/feed.xml"},
        state={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _signal(key: str = "e1") -> TriggerSignal:
    return TriggerSignal(
        kind=TriggerKind.RSS,
        source="RSS Changelog",
        headline="Scheduler picks its own window",
        summary="The scheduler now reads the project's own publishing history.",
        dedupe_key=key,
        suggested_type=ContentType.ANNOUNCEMENT,
    )


def _abandon(db, trigger, *, age_seconds: int, key: str = "e1") -> TriggerEvent:
    """A firing recorded and then never settled, aged past the cutoff."""
    event = triggers.record(db, trigger, _signal(key))
    assert event is not None
    event.created_at = utcnow() - timedelta(seconds=age_seconds)
    db.commit()
    return event


def _stale_seconds() -> int:
    """Comfortably past the cutoff, read from the setting rather than pinned."""
    return settings.trigger_event_stuck_after_seconds + 60


# --------------------------------------------------------------------------- #
# The link that makes the two cases distinguishable                            #
# --------------------------------------------------------------------------- #


def test_a_fired_trigger_stamps_its_event_id_on_the_piece(db, trigger):
    """Written inside the transaction that stores the piece.

    ``TriggerEvent.content_id`` points the other way and is written one commit
    later, which is exactly the gap this exists to survive.
    """
    event = triggers.fire(db, trigger, _signal())

    assert event is not None
    piece = db.query(Content).one()
    assert piece.source["event_id"] == event.id
    assert event.content_id == piece.id


# --------------------------------------------------------------------------- #
# Reclaiming                                                                   #
# --------------------------------------------------------------------------- #


def test_a_firing_interrupted_after_the_piece_was_written_adopts_it(db, trigger):
    """The crash landed between storing the piece and recording that it was.

    Calling this one "failed" would be a plain untruth: on a project set to
    ``auto`` the piece may already have published, and the firing's own page is
    the one place a user looks to find out.
    """
    event = _abandon(db, trigger, age_seconds=_stale_seconds())
    piece = Content(
        project_id=trigger.project_id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.REVIEW,
        title="Scheduler picks its own window",
        slug="scheduler-picks-its-own-window",
        body_markdown="## What changed\n\nA good deal.",
        source={"kind": "trigger", "event_id": event.id, "trigger_id": trigger.id},
    )
    db.add(piece)
    db.commit()

    assert triggers.reclaim_stuck_events(db) == 1

    db.refresh(event)
    assert event.status == TriggerEventStatus.GENERATED
    assert event.content_id == piece.id
    assert event.detail == triggers.ADOPTED_DETAIL


def test_a_firing_interrupted_before_anything_was_written_is_failed(db, trigger):
    event = _abandon(db, trigger, age_seconds=_stale_seconds())

    assert triggers.reclaim_stuck_events(db) == 1

    db.refresh(event)
    assert event.status == TriggerEventStatus.FAILED
    assert event.content_id is None
    assert event.detail == triggers.ABANDONED_DETAIL


def test_a_generation_still_running_is_left_alone(db, trigger):
    """The cutoff is the whole safety property.

    Inside it the worker is alive and the row is not stuck at all; settling it
    would overwrite the outcome that task is about to write.
    """
    event = _abandon(db, trigger, age_seconds=60)

    assert triggers.reclaim_stuck_events(db) == 0

    db.refresh(event)
    assert event.status == TriggerEventStatus.RECEIVED


def test_a_firing_that_settled_itself_is_left_alone(db, trigger):
    settled = triggers.fire(db, trigger, _signal())
    assert settled is not None
    settled.created_at = utcnow() - timedelta(seconds=_stale_seconds())
    db.commit()
    before = settled.detail

    assert triggers.reclaim_stuck_events(db) == 0

    db.refresh(settled)
    assert settled.status == TriggerEventStatus.GENERATED
    assert settled.detail == before


def test_one_sweep_settles_a_whole_batch(db, trigger):
    """A deploy restarts the worker once and takes every firing in flight."""
    written = _abandon(db, trigger, age_seconds=_stale_seconds(), key="a")
    db.add(
        Content(
            project_id=trigger.project_id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.REVIEW,
            title="One",
            slug="one",
            body_markdown="body",
            source={"kind": "trigger", "event_id": written.id},
        )
    )
    lost = [
        _abandon(db, trigger, age_seconds=_stale_seconds(), key=key)
        for key in ("b", "c")
    ]
    db.commit()

    assert triggers.reclaim_stuck_events(db) == 3

    db.refresh(written)
    assert written.status == TriggerEventStatus.GENERATED
    for event in lost:
        db.refresh(event)
        assert event.status == TriggerEventStatus.FAILED


def test_a_piece_from_another_firing_is_not_adopted(db, trigger):
    """The match is on the firing's own id, not on "a piece from this trigger".

    A trigger that fires hourly has plenty of pieces to be wrongly credited
    with, and crediting one to the firing that lost its worker would hide the
    loss behind somebody else's work.
    """
    event = _abandon(db, trigger, age_seconds=_stale_seconds())
    db.add(
        Content(
            project_id=trigger.project_id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.REVIEW,
            title="A different piece",
            slug="a-different-piece",
            body_markdown="body",
            source={"kind": "trigger", "event_id": event.id + 999,
                    "trigger_id": trigger.id},
        )
    )
    db.commit()

    triggers.reclaim_stuck_events(db)

    db.refresh(event)
    assert event.status == TriggerEventStatus.FAILED


def test_a_manual_piece_is_never_mistaken_for_a_firings_work(db, trigger):
    """``source`` has no ``event_id`` on anything a human wrote."""
    event = _abandon(db, trigger, age_seconds=_stale_seconds())
    db.add(
        Content(
            project_id=trigger.project_id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.DRAFT,
            title="Typed by hand",
            slug="typed-by-hand",
            body_markdown="body",
            source={"kind": "manual", "user_id": 1},
        )
    )
    db.commit()

    triggers.reclaim_stuck_events(db)

    db.refresh(event)
    assert event.status == TriggerEventStatus.FAILED


# --------------------------------------------------------------------------- #
# Why it is settled rather than retried                                        #
# --------------------------------------------------------------------------- #


def test_the_lost_signal_cannot_come_back_on_its_own(db, trigger):
    """Which is why a terminal status with a reason on it is worth so much.

    The sender redelivering, or the next poll re-listing the entry, collides
    with the dedupe key the abandoned firing already spent. Nothing about
    settling the row changes that — the point is that the loss becomes visible
    instead of looking like a firing that is still going.
    """
    event = _abandon(db, trigger, age_seconds=_stale_seconds())
    triggers.reclaim_stuck_events(db)

    again = triggers.record(db, trigger, _signal())

    assert again is None
    db.refresh(event)
    assert event.status == TriggerEventStatus.FAILED


# --------------------------------------------------------------------------- #
# The sweep that calls it                                                      #
# --------------------------------------------------------------------------- #


def test_the_trigger_sweep_settles_abandoned_firings_before_it_dispatches(
    db, trigger, monkeypatch
):
    """Nothing else will: the sweep never rediscovers a firing it lost."""
    monkeypatch.setattr(trigger_tasks, "SessionLocal", lambda: _NoClose(db))
    event = _abandon(db, trigger, age_seconds=_stale_seconds())

    result = trigger_tasks.check_due_triggers()

    assert result["reclaimed"] == 1
    db.refresh(event)
    assert event.status == TriggerEventStatus.FAILED


class _NoClose:
    """The test session, wrapped so a task's ``db.close()`` does not end it."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass
