"""The one place a timeout can silently become *no* timeout.

``db_statement_timeout_seconds`` is the server-side ceiling on any single
statement — the backstop for the case the pool timeout cannot reach, where a
query was legitimately checked out and then simply never came back. It is
configured in seconds because that is what an operator thinks in, and PostgreSQL
takes it in milliseconds, so there is a conversion in the middle.

That conversion is the whole risk. ``statement_timeout=0`` is PostgreSQL's
spelling for *no limit*, so any arithmetic that can round down to zero turns the
tightest setting an operator could ask for into the loosest one available, and
does it without an error, a log line, or any difference the engine could
notice. :func:`~app.database._statement_timeout_option` rounds up for exactly
that reason, and nothing asserted it.

The tests below pin the three things the conversion has to get right — the unit,
the direction it rounds, and which value is allowed to mean "off" — plus the one
thing the caller depends on: that "off" is expressed by omitting the option
rather than by sending a zero.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.database import _statement_timeout_option


def _timeout_ms(seconds: float) -> int:
    """The number PostgreSQL is actually told, parsed back out of the option."""
    (option,) = _statement_timeout_option(seconds).values()
    prefix = "-c statement_timeout="
    assert option.startswith(prefix), option
    return int(option.removeprefix(prefix))


def test_seconds_are_converted_to_milliseconds():
    """The unit, asserted once so the rest can talk in milliseconds."""
    assert _timeout_ms(30.0) == 30_000
    assert _timeout_ms(1.0) == 1_000


@pytest.mark.parametrize("seconds", [0.0001, 0.0005, 0.001, 0.4])
def test_a_sub_second_timeout_never_rounds_down_to_no_timeout(seconds):
    """The failure this function is shaped around.

    Truncating instead of rounding up turns a tenth of a millisecond into
    ``statement_timeout=0``, which does not mean "immediately" — it means
    *never*. The operator asks for the strictest ceiling available and gets no
    ceiling at all, on a code path with no error to notice.
    """
    assert _timeout_ms(seconds) >= 1


def test_rounding_up_never_makes_the_ceiling_looser_than_asked_for():
    """Up rather than nearest, and the difference is the direction of the error.

    A ceiling that rounds down is a promise broken quietly; one that rounds up
    is at most a millisecond of slack. 1.5 ms cannot become 1.
    """
    assert _timeout_ms(0.0015) == 2
    assert _timeout_ms(2.0001) == 2001


def test_zero_disables_the_ceiling_by_saying_nothing_at_all():
    """Zero is the documented "off", and off has to be an *absent* option.

    Passing ``statement_timeout=0`` through would also disable it, so this looks
    like a distinction without a difference — except that the return value is
    handed to ``create_engine`` as ``connect_args``, and an empty mapping is the
    only form of "off" that leaves the connection string exactly as it would
    have been if the feature did not exist.
    """
    assert _statement_timeout_option(0) == {}
    assert _statement_timeout_option(0.0) == {}


def test_a_negative_setting_disables_rather_than_sending_nonsense():
    """Unreachable through config, and cheap to be right about anyway.

    ``Settings`` refuses a negative value, so this is defence in depth for a
    direct caller. What matters is that it fails towards "no option" rather than
    towards ``statement_timeout=-1``, which PostgreSQL rejects at connect time —
    turning a bad number into a database that will not accept connections.
    """
    assert _statement_timeout_option(-1.0) == {}


def test_the_configured_default_produces_a_real_ceiling():
    """The setting and the conversion, joined up.

    Each is fine alone and the pair is what ships: the default has to survive
    the conversion as a bound, not as a zero.
    """
    default = Settings(jwt_secret="x" * 40).db_statement_timeout_seconds

    assert default > 0
    assert _timeout_ms(default) == pytest.approx(default * 1000, abs=1)
