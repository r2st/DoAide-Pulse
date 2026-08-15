"""Reaching PostgreSQL had no time limit, and the health probe inherited it.

Herald bounds a *query* — ``statement_timeout`` is sent at connect time so every
checkout carries it — and bounds *waiting for a pooled connection*, via
``pool_timeout``. Neither of those covers opening a connection in the first
place, which is the step that fails when a database goes away.

And it fails slowly. A refused connection comes back instantly; a box that is up
but dropping packets — a firewall change, a failover mid-flight, the network
partition that actually happens — does not answer at all, and libpq's default is
to leave the open in the kernel's TCP retry schedule for over two minutes.

Which makes it a health-check bug before it is a request bug. ``/health`` runs
``SELECT 1`` on a pooled session, and ``pool_pre_ping`` discards a connection
that went stale and opens a replacement — so the probe blocks on that connect.
Caddy polls the same endpoint every 30 seconds as its ``health_uri``, so the
intended outcome (a fast 503 naming the unreachable dependency, the upstream
ejected, an operator pointed at PostgreSQL) was instead a probe that never
answered, uvicorn workers stuck in a connect, and a site that timed out without
saying why.

The bound is one connection parameter. These tests pin it, the two ways it can
be wrong — a value libpq silently reinterprets, and one it reads as *no* bound —
and the fact that it does not reach SQLite, which has nothing to connect to.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from app.config import Settings
from app.database import (
    MIN_CONNECT_TIMEOUT_SECONDS,
    _connect_options,
    _connect_timeout_arg,
    _make_engine,
)

#: A URL that names a database nothing will connect to. ``create_engine`` is
#: lazy — nothing opens a socket until a statement runs — so the connect args
#: can be read off a real engine without one existing.
_POSTGRES = "postgresql+psycopg://herald:herald@db.invalid:5432/herald"


class _Enough(Exception):
    """Raised from a ``do_connect`` listener once it has seen the parameters.

    Stops the attempt before it reaches a host that does not exist, so the test
    costs nothing and cannot depend on how long a DNS failure takes here.
    """


def test_a_connection_attempt_is_bounded():
    assert _connect_timeout_arg(5.0) == {"connect_timeout": 5}


def test_a_bound_below_libpq_s_floor_is_raised_to_it():
    """``connect_timeout=1`` is read as 2 by libpq, silently.

    Configuration that does not mean what it says is worse than configuration
    that is missing: it reads as deliberate. Anything under the floor is raised
    to it here, where the value is chosen, rather than inside the driver.
    """
    assert _connect_timeout_arg(1.0) == {"connect_timeout": MIN_CONNECT_TIMEOUT_SECONDS}
    assert _connect_timeout_arg(0.25) == {"connect_timeout": MIN_CONNECT_TIMEOUT_SECONDS}


def test_a_fractional_bound_rounds_up_rather_than_down():
    """libpq's parameter is whole seconds. Rounding down would turn 2.5 into a
    tighter bound than was asked for, and this is a setting somebody reaches for
    when connects are already marginal."""
    assert _connect_timeout_arg(4.2) == {"connect_timeout": 5}


def test_zero_is_the_documented_way_to_ask_for_no_bound():
    """libpq's own spelling, matching ``db_statement_timeout_seconds``.

    Absent rather than ``0``: passing the parameter explicitly as zero says the
    same thing to libpq, but leaving it out is what "Herald is not setting this"
    should look like in the connect args somebody is reading during an incident.
    """
    assert _connect_timeout_arg(0) == {}
    assert _connect_timeout_arg(-1) == {}


def test_the_bound_reaches_the_driver_on_a_real_postgres_engine():
    """The arithmetic above is only worth anything if it is wired in.

    Read off the ``do_connect`` event rather than off the dialect, because they
    are not the same dictionary: ``create_connect_args`` returns what the *URL*
    implies, and the ``connect_args`` handed to ``create_engine`` are merged
    over it inside the pool's creator. The event is the last point before the
    driver, so it is the only place the merged result exists.
    """
    engine = _make_engine(_POSTGRES)
    seen: dict[str, object] = {}

    @event.listens_for(engine, "do_connect")
    def _capture(_dialect, _record, _cargs, cparams):
        seen.update(cparams)
        raise _Enough

    with pytest.raises(_Enough):
        engine.connect()

    assert seen["connect_timeout"] == 5
    # And has not displaced the two settings that were already there — they
    # travel in a separate libpq key, which is what a careless merge would drop.
    assert "timezone=UTC" in seen["options"]
    assert "statement_timeout" in seen["options"]


def test_the_two_libpq_settings_still_share_one_options_string():
    """Unchanged, and asserted here because the merge above is next to it:
    libpq takes a single ``options``, so two keys would silently keep one."""
    options = _connect_options(30.0)["options"]

    assert options == "-c timezone=UTC -c statement_timeout=30000"


def test_sqlite_is_left_alone():
    """The test database is a file handle, not a socket. Passing a libpq
    parameter to it is a ``TypeError`` at connect time, and every test in the
    suite would be the one that found out."""
    engine = _make_engine("sqlite://")

    connect_args = engine.dialect.create_connect_args(engine.url)[1]

    assert "connect_timeout" not in connect_args
    assert "options" not in connect_args


def test_the_setting_has_a_default_that_is_shorter_than_caddy_s_interval():
    """Caddy re-probes ``/health`` every 30 seconds. A connect bound anywhere
    near that would let a single unreachable database hold a probe past the next
    one, which is the pile-up the bound exists to prevent."""
    assert 0 < Settings().db_connect_timeout_seconds < 30


def test_a_negative_bound_is_refused_at_startup():
    """Not a slow connect — a nonsensical one. It would reach libpq as a
    rounded-up negative, which is neither the bound asked for nor an obvious
    error at the point it is read."""
    with pytest.raises(ValidationError):
        Settings(db_connect_timeout_seconds=-1.0)
