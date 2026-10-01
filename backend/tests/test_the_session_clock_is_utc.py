"""Pulse has one clock, and nothing was insisting on it at the connection.

Every timestamp column is ``DateTime(timezone=True)`` — ``timestamptz`` — and a
driver reading one converts it to the **session** time zone before handing it
back. That zone comes from ``postgresql.conf``, the role, or the server's own
locale, and no code in this repo was setting it. On a box that is not on UTC
every timestamp therefore arrived shifted, and the code that reads a field off
one read the wrong field: ``captured_at.date()`` returned the local day, and
``published.hour`` — which :mod:`app.services.learned_cadence` learns a
publishing rhythm from and hands to a scheduler that means UTC — returned the
local hour.

None of that can be reproduced against the test database. SQLite has no time
zones and returns the naive UTC that was written, so the bug existed only where
it was expensive. What *can* be pinned is the connection parameter that stops
it, and the bucket key that no longer depends on it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.database import _connect_options, _statement_timeout_option
from app.services import analytics_service


def _options(seconds: float = 30.0) -> str:
    (option,) = _connect_options(seconds).values()
    return option


# --------------------------------------------------------------------------- #
# The connection                                                               #
# --------------------------------------------------------------------------- #


def test_every_connection_asks_for_utc():
    assert "-c timezone=UTC" in _options()


def test_turning_the_statement_timeout_off_does_not_turn_the_clock_off_with_it():
    """The timeout is optional; the clock is not.

    ``_statement_timeout_option`` expresses "no limit" by returning no option at
    all, so composing the two by asking it first would have dropped the whole
    ``options`` string along with it.
    """
    assert _statement_timeout_option(0) == {}
    assert "-c timezone=UTC" in _options(0)


def test_both_settings_travel_in_the_one_string_libpq_reads():
    """libpq takes a single ``options``; two keys would keep only the last."""
    option = _options(30.0)
    assert "-c timezone=UTC" in option
    assert "-c statement_timeout=30000" in option
    assert len(_connect_options(30.0)) == 1


# --------------------------------------------------------------------------- #
# The bucket key                                                               #
# --------------------------------------------------------------------------- #


def test_a_timestamp_that_arrives_shifted_is_still_filed_under_its_utc_day():
    """The same instant, told three ways. One day.

    ``value.date()`` is the date in whatever zone the value carries, which is
    what made this depend on the server's configuration at all.
    """
    instant = datetime(2026, 8, 14, 23, 30, tzinfo=UTC)

    assert analytics_service.utc_day(instant) == "2026-08-14"
    # Berlin: the same moment, already tomorrow by the wall clock.
    assert analytics_service.utc_day(instant.astimezone(timezone(timedelta(hours=2)))) == (
        "2026-08-14"
    )
    # A naive value is what SQLite returns, and it is UTC by construction.
    assert analytics_service.utc_day(instant.replace(tzinfo=None)) == "2026-08-14"


def test_a_reading_at_the_end_of_the_window_is_not_shifted_out_of_the_chart():
    """The failure this cost: not a wrong bucket, a missing one.

    The labels come from :func:`analytics_service.window_start` and are UTC by
    construction. A reading shifted into the day *after* the last label matches
    no bucket at all and leaves the chart without trace — a silent subtraction
    on the one screen whose job is to be counted on.
    """
    days = 30
    since = analytics_service.window_start(days)
    labels = {
        (since.date() + timedelta(days=offset)).isoformat() for offset in range(days + 1)
    }

    # The last instant the window covers, seen from a zone ahead of UTC.
    latest = since + timedelta(days=days, hours=23, minutes=59)
    shifted = latest.astimezone(timezone(timedelta(hours=13)))

    assert shifted.date().isoformat() not in labels
    assert analytics_service.utc_day(shifted) in labels
