"""A schedule pinned to an hour must not drift out of it.

``last_fired_at`` is stamped when the *worker* runs, not when beat decided the
trigger was due — so it is the poll instant plus however long generating a piece
took. Judging "every 24 hours" against that stamp makes every firing land a
little later than the last, and the pinned hour is a 60-minute window the drift
eventually walks out of: the day is skipped, the phase resets a poll interval
later, and the cycle repeats.

The user-visible symptom is the one worth naming in a test: a trigger the UI
calls *daily* that quietly produces six pieces a week. These pin the behaviour
at both ends — the arithmetic that used to drift, and the interval shapes that
must keep the elapsed-hours reading they already had.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.trigger import Trigger, TriggerKind
from app.services import triggers

#: A ten-minute sweep, matching ``settings.trigger_scan_interval_seconds``.
POLL = timedelta(seconds=600)
#: Time between beat deciding a trigger is due and the worker stamping the row.
WORKER_LATENCY = timedelta(seconds=45)


def _schedule(db, project, **config) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.SCHEDULE,
        name="Daily",
        config=config,
        state={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _run_sweep(trigger, *, days: int, start: datetime) -> list[datetime]:
    """Poll every ten minutes for *days* and return when the trigger fired.

    Mirrors what beat and the worker actually do: ``is_due`` is asked at each
    poll instant, and a firing stamps ``last_fired_at`` a worker-latency later.
    """
    now, end = start, start + timedelta(days=days)
    fired: list[datetime] = []
    while now < end:
        if triggers.is_due(trigger, moment=now):
            trigger.last_fired_at = now + WORKER_LATENCY
            fired.append(trigger.last_fired_at)
        now += POLL
    return fired


def test_a_daily_pinned_schedule_fires_every_day_for_a_year(db, project):
    """The regression: worker latency used to cost one day in seven."""
    trigger = _schedule(db, project, every_hours=24, hour_utc=9)

    fired = _run_sweep(trigger, days=365, start=datetime(2026, 1, 1, tzinfo=UTC))

    assert len(fired) == 365
    # One per calendar day, with no day claimed twice.
    assert len({moment.date() for moment in fired}) == 365


def test_a_daily_pinned_schedule_does_not_drift_out_of_its_hour(db, project):
    """Every firing lands at the same wall-clock time, a year apart included."""
    trigger = _schedule(db, project, every_hours=24, hour_utc=9)

    fired = _run_sweep(trigger, days=365, start=datetime(2026, 1, 1, tzinfo=UTC))

    assert {moment.timetz() for moment in fired} == {
        datetime(2026, 1, 1, 9, 0, 45, tzinfo=UTC).timetz()
    }


def test_a_weekly_pinned_schedule_fires_every_seven_days(db, project):
    """168 hours is seven days, and must stay seven rather than seven-and-a-bit."""
    trigger = _schedule(db, project, every_hours=168, hour_utc=9)

    fired = _run_sweep(trigger, days=364, start=datetime(2026, 1, 1, tzinfo=UTC))

    assert len(fired) == 52
    gaps = {(b.date() - a.date()).days for a, b in zip(fired, fired[1:], strict=False)}
    assert gaps == {7}


def test_the_day_count_is_the_schedule_not_the_elapsed_hours(db, project):
    """Short of a full 24 hours, but on the next day and in the hour: due.

    This is the exact comparison that used to fail — the case worker latency
    manufactures on every single firing.
    """
    trigger = _schedule(db, project, every_hours=24, hour_utc=9)
    trigger.last_fired_at = datetime(2026, 3, 1, 9, 0, 45, tzinfo=UTC)
    db.commit()

    assert triggers.is_due(trigger, moment=datetime(2026, 3, 2, 9, 0, tzinfo=UTC)) is True


def test_a_pinned_schedule_still_waits_out_the_whole_day(db, project):
    """Counting days must not become "fires whenever the hour comes round"."""
    trigger = _schedule(db, project, every_hours=168, hour_utc=9)
    trigger.last_fired_at = datetime(2026, 3, 1, 9, 0, 45, tzinfo=UTC)
    db.commit()

    # Same day, and every day short of the seventh.
    for day in range(1, 8):
        moment = datetime(2026, 3, day, 9, 30, tzinfo=UTC)
        assert triggers.is_due(trigger, moment=moment) is False, day

    assert triggers.is_due(trigger, moment=datetime(2026, 3, 8, 9, 0, tzinfo=UTC)) is True


def test_the_hour_gate_still_closes_every_other_hour(db, project):
    """Day counting replaces the window, not the pin."""
    trigger = _schedule(db, project, every_hours=24, hour_utc=9)
    trigger.last_fired_at = datetime(2026, 3, 1, 9, 0, 45, tzinfo=UTC)
    db.commit()

    for hour in (8, 10, 0, 23):
        moment = datetime(2026, 3, 2, hour, 0, tzinfo=UTC)
        assert triggers.is_due(trigger, moment=moment) is False, hour


@pytest.mark.parametrize("every_hours", [36, 30, 25])
def test_a_part_day_interval_keeps_the_elapsed_hours_reading(db, project, every_hours):
    """No whole number of days to count, so the old comparison still governs."""
    trigger = _schedule(db, project, every_hours=every_hours, hour_utc=9)
    trigger.last_fired_at = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    db.commit()

    # The next day's pinned hour is 24 hours on — short of every interval here.
    assert triggers.is_due(trigger, moment=datetime(2026, 3, 3, 9, 0, tzinfo=UTC)) is False
    # Two days on clears all of them.
    assert triggers.is_due(trigger, moment=datetime(2026, 3, 4, 9, 0, tzinfo=UTC)) is True


def test_an_unpinned_daily_schedule_keeps_the_elapsed_hours_reading(db, project):
    """With no pinned hour there is no gate holding it to one firing a day."""
    trigger = _schedule(db, project, every_hours=24)
    trigger.last_fired_at = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    db.commit()

    assert triggers.is_due(trigger, moment=datetime(2026, 3, 3, 8, 0, tzinfo=UTC)) is False
    assert triggers.is_due(trigger, moment=datetime(2026, 3, 3, 9, 0, tzinfo=UTC)) is True


def test_a_pinned_schedule_that_has_never_fired_is_due_in_its_hour(db, project):
    """A fresh trigger has no day to count from and must not wait a cycle."""
    trigger = _schedule(db, project, every_hours=24, hour_utc=9)

    assert triggers.is_due(trigger, moment=datetime(2026, 3, 2, 9, 0, tzinfo=UTC)) is True
    assert triggers.is_due(trigger, moment=datetime(2026, 3, 2, 8, 0, tzinfo=UTC)) is False
