"""The slow-query log, and the values it must never contain.

Herald has three database timeouts and all three are ceilings: they say what it
refuses to wait for, and by the time one fires the request it was protecting is
already lost. The query that takes four seconds every time and *succeeds* is
invisible to every one of them, and invisible to the test suite too — an N+1
shows up there as a query count (``conftest.sql_log``), but a query that is slow
because production has a hundred thousand rows shows up nowhere at all.

The second half of this file is the more important one. SQLAlchemy hands the
bound parameters to the same hook as the statement, and the writes this schema
does bind password hashes, session tokens, encrypted platform credentials and
every user's email address. Logging them would put all of that in a file that
ships wherever logs ship to, in exchange for information that does not help:
what identifies a query needing an index is its *shape*.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, event, text

from app import database


@pytest.fixture
def engine():
    """A throwaway engine, so listeners cannot outlive the test.

    The module-level engine is shared by the whole suite and installing a
    logger on it would leave one behind — ``install_slow_query_logging`` is
    idempotent per engine precisely so that cannot happen twice, but the tidy
    thing is not to touch it at all.
    """
    made = create_engine("sqlite://", future=True)
    try:
        yield made
    finally:
        made.dispose()


def _run(made, statement: str = "SELECT 1") -> None:
    with made.connect() as conn:
        conn.execute(text(statement))


# --------------------------------------------------------------------------- #
# Arming                                                                       #
# --------------------------------------------------------------------------- #


def test_a_zero_threshold_installs_nothing(engine):
    """The opt-out has to actually opt out, not log everything.

    A threshold of zero read as "log statements over 0ms" is every statement
    the process runs, which on a busy install is a way to fill a disk.
    """
    assert database.install_slow_query_logging(engine, 0) is False


def test_it_is_not_installed_twice(engine):
    """Or a slow query is reported twice and the second one looks like a second query."""
    assert database.install_slow_query_logging(engine, 1) is True
    assert database.install_slow_query_logging(engine, 1) is False


def test_the_shipped_engine_is_armed():
    """The wiring, not the mechanism — the two are easy to get separately right."""
    assert getattr(database.engine, "_herald_slow_query_logging", False) is True


# --------------------------------------------------------------------------- #
# What it logs                                                                 #
# --------------------------------------------------------------------------- #


def test_a_query_over_the_threshold_is_logged(engine, caplog):
    database.install_slow_query_logging(engine, 1)

    with caplog.at_level(logging.WARNING, logger="app.database"):
        # A threshold of 1ms and a statement that sleeps past it. SQLite has no
        # sleep, so the wait is done in an event handler that runs inside the
        # measured window.
        @event.listens_for(engine, "before_cursor_execute")
        def _stall(conn, cursor, statement, parameters, context, executemany):
            import time

            time.sleep(0.01)

        _run(engine)

    assert any("slow query" in record.message for record in caplog.records)


def test_a_fast_query_is_not(engine, caplog):
    """A log that reports every query is a log nobody reads."""
    database.install_slow_query_logging(engine, 60_000)

    with caplog.at_level(logging.WARNING, logger="app.database"):
        _run(engine)

    assert not [r for r in caplog.records if "slow query" in r.message]


def test_the_line_carries_the_duration_and_the_statement(engine, caplog):
    """Both, because either alone is unactionable.

    A duration with no statement says the install is slow somewhere; a
    statement with no duration says nothing at all.
    """
    database.install_slow_query_logging(engine, 1)

    @event.listens_for(engine, "before_cursor_execute")
    def _stall(conn, cursor, statement, parameters, context, executemany):
        import time

        time.sleep(0.01)

    with caplog.at_level(logging.WARNING, logger="app.database"):
        _run(engine, "SELECT 42 AS answer")

    line = next(r.getMessage() for r in caplog.records if "slow query" in r.message)
    assert "ms" in line
    assert "SELECT 42 AS answer" in line


def test_the_parameters_are_never_logged(engine, caplog):
    """The reason a naive version of this is a security bug.

    The bound values of the writes this schema does include password hashes,
    session tokens and encrypted platform credentials. The statement's shape is
    what identifies a query that needs an index; the values are no help with
    that and a great deal of harm in a log file.
    """
    database.install_slow_query_logging(engine, 1)

    @event.listens_for(engine, "before_cursor_execute")
    def _stall(conn, cursor, statement, parameters, context, executemany):
        import time

        time.sleep(0.01)

    with (
        caplog.at_level(logging.WARNING, logger="app.database"),
        engine.connect() as conn,
    ):
        conn.execute(
            text("SELECT :secret AS leaked"),
            {"secret": "hunter2-not-in-the-log"},
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "slow query" in logged
    assert "hunter2-not-in-the-log" not in logged


def test_a_long_statement_is_truncated(engine, caplog):
    """One slow ORM insert must not push the rest of the log off the screen."""
    database.install_slow_query_logging(engine, 1)

    @event.listens_for(engine, "before_cursor_execute")
    def _stall(conn, cursor, statement, parameters, context, executemany):
        import time

        time.sleep(0.01)

    columns = ", ".join(f"{n} AS c{n}" for n in range(200))
    with caplog.at_level(logging.WARNING, logger="app.database"):
        _run(engine, f"SELECT {columns}")

    line = next(r.getMessage() for r in caplog.records if "slow query" in r.message)
    assert "…" in line
    assert len(line) < database.SLOW_QUERY_STATEMENT_CHARS + 200


def test_the_statement_is_flattened_to_one_line(engine):
    """A multi-line ORM query would otherwise be twenty log lines."""
    assert database._condensed("SELECT 1\n  FROM t\n  WHERE x = 2") == (
        "SELECT 1 FROM t WHERE x = 2"
    )
