"""The day the scheduler was throwing away.

``next_slot`` advanced a day before it looked at its first candidate, so the
day the caller was standing in could never be chosen — however good a day the
rota thought it was, and however many hours were left in it. Every caller
passes "now" as ``after`` (the optimiser, the calendar sidebar, the suggestion
endpoint), so the effect was uniform: ask on a Tuesday morning for the best
time to post on dev.to, whose rota is Tue–Thu at 13:00 UTC, and be told
Wednesday while Tuesday 13:00 sat four hours away.

The bar for a same-day slot is only that its hour has not gone yet. The 12-hour
spacing rule against ``taken`` is what stops a batch stacking up, and it is
unchanged — these tests pin both halves, because a fix for the first that broke
the second would put three posts on one morning.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.publication import Platform
from app.services import cadence

# A Tuesday. Dev.to's rota is Tue–Thu at 13:00 UTC, so this day is one the
# table already wants and the hour is the one it names.
TUESDAY = datetime(2026, 8, 11, tzinfo=UTC)


def _at(hour: int, *, day: datetime = TUESDAY) -> datetime:
    return day.replace(hour=hour)


def test_todays_slot_is_taken_when_its_hour_has_not_gone_yet():
    slot = cadence.next_slot(Platform.DEVTO, after=_at(9))

    assert slot == _at(13), "13:00 today is four hours away and on the rota"


def test_a_slot_already_gone_today_moves_to_the_next_rota_day():
    slot = cadence.next_slot(Platform.DEVTO, after=_at(14))

    assert slot == _at(13) + timedelta(days=1)


def test_the_hour_landed_on_exactly_is_not_offered_back():
    """"The next good time after X" cannot be X — that moment is now."""
    slot = cadence.next_slot(Platform.DEVTO, after=_at(13))

    assert slot > _at(13)
    assert slot == _at(13) + timedelta(days=1)


def test_a_day_off_the_rota_still_walks_forward_to_one_on_it():
    sunday = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    slot = cadence.next_slot(Platform.DEVTO, after=sunday)

    assert slot.weekday() in cadence.cadence_for(Platform.DEVTO).best_weekdays
    assert slot == _at(13)


def test_today_is_still_refused_when_something_is_already_there():
    """The spacing rule outranks the same-day preference, not the reverse."""
    slot = cadence.next_slot(
        Platform.DEVTO, after=_at(9), taken=[_at(12)]
    )

    assert slot >= _at(13) + timedelta(days=1)
    assert abs((slot - _at(12)).total_seconds()) >= 12 * 3600


def test_a_batch_starting_today_is_still_spread_out():
    """The gain is a day off the front, not three posts on one morning."""
    slots = cadence.suggest_schedule(Platform.DEVTO, count=3, start=_at(9))

    assert slots[0] == _at(13), "the batch starts today"
    assert len(set(slots)) == 3
    gaps = [(b - a).total_seconds() for a, b in zip(slots, slots[1:], strict=False)]
    assert all(gap >= 12 * 3600 for gap in gaps)


def test_every_platform_can_use_the_day_it_is_asked_on():
    """Not a dev.to quirk: no platform's rota may silently skip today."""
    for platform in Platform:
        table = cadence.cadence_for(platform)
        # Wind to a day the platform's own rota likes, and stand an hour before
        # its hour.
        day = TUESDAY
        while day.weekday() not in table.best_weekdays:
            day += timedelta(days=1)
        after = day.replace(hour=max(0, table.best_hour_utc - 1))

        slot = cadence.next_slot(platform, after=after)
        assert slot.date() == day.date(), f"{platform.value} skipped its own day"
        assert slot.hour == table.best_hour_utc


def test_the_search_still_gives_up_rather_than_looping_for_ever():
    """A rota no day satisfies must return something, not spin."""
    impossible = cadence.Cadence(
        platform=Platform.DEVTO,
        max_per_week=1,
        best_weekdays=(),
        best_hour_utc=13,
        rationale="nothing is a good day",
    )
    slot = cadence.next_slot(Platform.DEVTO, after=_at(9), using=impossible)

    assert slot.hour == 13
    assert slot > _at(9)
