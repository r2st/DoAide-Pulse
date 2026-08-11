"""Two pieces of wiring the test suite substitutes for, and so never runs.

``conftest`` overrides :func:`app.database.get_db` with a fixture-owned session
and lets exceptions propagate out of ``TestClient`` so failures show a
traceback. Both are the right thing for a test suite to do, and both mean the
production versions of those paths have never executed — the session that is
supposed to be closed after every request, and the handler that decides what a
caller sees when something raises where nothing caught it.

Neither is exotic code, and that is the point: they run on every single request
in production and on none of them here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import database
from app.config import settings
from app.main import create_app


class _SpySession:
    """Stands in for a real session so "was it closed" is answerable.

    A closed SQLAlchemy session is still usable — it opens a new transaction on
    the next statement — so there is no state on the real object that
    distinguishes "closed once" from "never closed".
    """

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def spy_session(monkeypatch) -> _SpySession:
    spy = _SpySession()
    monkeypatch.setattr(database, "SessionLocal", lambda: spy)
    return spy


def test_the_request_session_is_closed_when_the_request_ends(spy_session):
    generator = database.get_db()

    assert next(generator) is spy_session
    assert spy_session.closed == 0

    with pytest.raises(StopIteration):
        next(generator)
    assert spy_session.closed == 1


def test_the_request_session_is_closed_when_the_request_raises(spy_session):
    """The ``finally`` is the whole reason the dependency is a generator.

    Without it a handler that raises leaks its connection back to nothing, and
    under load the pool is exhausted by exactly the requests that were already
    going wrong.
    """
    generator = database.get_db()
    next(generator)

    with pytest.raises(RuntimeError, match="handler blew up"):
        generator.throw(RuntimeError("handler blew up"))

    assert spy_session.closed == 1


def test_get_db_hands_out_a_real_session_when_nothing_is_patched():
    """The spy above proves the closing; this proves what is being closed."""
    generator = database.get_db()
    try:
        assert isinstance(next(generator), Session)
    finally:
        generator.close()


@pytest.fixture
def production_shaped_app(monkeypatch):
    """An app built the way the deployed one is, with a route that raises.

    ``debug`` has to be off, and not as a detail of the fixture: Starlette's
    ``ServerErrorMiddleware`` checks ``debug`` *before* it reaches the
    registered handler, and when it is on it returns its own plain-text
    traceback instead. The repo's ``.env`` carries ``DEBUG=true`` and the test
    suite inherits it, so an app built here without this is the one shape in
    which the handler under test cannot run.
    """
    monkeypatch.setattr(settings, "debug", False)
    app = create_app()

    @app.get("/_raises_for_this_test")
    def _boom() -> None:
        raise RuntimeError("connection string postgresql://herald:hunter2@db")

    return app


def test_an_unhandled_exception_becomes_a_flat_500_with_nothing_in_it(
    production_shaped_app,
):
    """What a caller sees when a handler raises something nobody caught.

    ``raise_server_exceptions=False`` is what makes this reachable at all —
    ``TestClient``'s default is to re-raise so a failing test shows a
    traceback, which is why every other test in the suite goes straight past
    this handler.

    The assertion that matters is the negative one: nothing about the exception
    may reach the client. Starlette's own fallback is a bare
    ``Internal Server Error`` and FastAPI's is the exception message; this
    handler exists so neither is what ships.
    """
    with TestClient(production_shaped_app, raise_server_exceptions=False) as client:
        resp = client.get("/_raises_for_this_test")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    assert "postgresql" not in resp.text
    assert "RuntimeError" not in resp.text


def test_the_flat_500_is_logged_with_the_request_id(production_shaped_app, caplog):
    """The detail has to go somewhere, and the somewhere has to be joinable.

    ``request_id`` is the only thing tying the log line to the response the
    user is complaining about — the body deliberately carries nothing else.
    """
    with (
        caplog.at_level("ERROR", logger="app.main"),
        TestClient(production_shaped_app, raise_server_exceptions=False) as client,
    ):
        resp = client.get("/_raises_for_this_test")

    assert resp.status_code == 500
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "unhandled exception" in logged
    assert "request_id=" in logged
    # The traceback the response withheld is in the log, where it belongs.
    assert "postgresql://herald:hunter2@db" in caplog.text
