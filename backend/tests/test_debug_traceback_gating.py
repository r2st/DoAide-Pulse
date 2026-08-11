"""DEBUG must not turn unhandled exceptions into traceback pages in production.

``DEBUG=true`` is documented as the way to reopen /docs on a live box, and that
is how an operator reads it. But Starlette's ``ServerErrorMiddleware`` checks
the same flag *before* it consults an installed 500 handler, so setting it also
takes Herald's catch-all out of circuit and serves the traceback — source lines,
frame locals, whatever a DB error is carrying — to the caller.

The two uses of the flag are separable. These tests pin that separation: the
docs override survives in production, the traceback override does not, and
development keeps tracebacks because that is the point of development.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app, docs_enabled, traceback_responses_enabled

BOOM = "sentinel-secret-in-the-traceback"


@pytest.fixture
def exploding_client():
    """A client over a fresh app carrying one route that always raises.

    ``raise_server_exceptions=False`` makes TestClient behave like a real
    server: it returns whatever the middleware produced instead of re-raising,
    which is the only way to see *what the caller would have received*.
    """

    def build():
        app = create_app()

        @app.get("/__boom")
        async def _boom():
            raise RuntimeError(BOOM)

        return TestClient(app, raise_server_exceptions=False)

    return build


@pytest.mark.parametrize("environment", ["production", "PROD"])
def test_production_debug_still_returns_a_clean_500(
    exploding_client, monkeypatch, environment
):
    """The regression this guards: DEBUG=true on a prod box leaking a traceback."""
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "debug", True)
    assert traceback_responses_enabled() is False

    with exploding_client() as client:
        response = client.get("/__boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    # Nothing of the exception reaches the caller — not the message, not the
    # frames, not the module that raised.
    assert BOOM not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_production_debug_keeps_the_documented_docs_override(monkeypatch):
    """Closing the traceback leak must not close the escape hatch with it."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", True)

    assert docs_enabled() is True
    assert traceback_responses_enabled() is False

    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_production_without_debug_returns_a_clean_500(exploding_client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", False)
    assert traceback_responses_enabled() is False

    with exploding_client() as client:
        response = client.get("/__boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert BOOM not in response.text


def test_development_debug_still_gets_tracebacks(exploding_client, monkeypatch):
    """Development is where the traceback is the whole point."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "debug", True)
    assert traceback_responses_enabled() is True

    with exploding_client() as client:
        response = client.get("/__boom")

    assert response.status_code == 500
    assert BOOM in response.text


def test_development_without_debug_returns_a_clean_500(exploding_client, monkeypatch):
    """The flag, not the environment, is what opens tracebacks."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "debug", False)
    assert traceback_responses_enabled() is False

    with exploding_client() as client:
        response = client.get("/__boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert BOOM not in response.text


def test_the_request_id_survives_the_catch_all(exploding_client, monkeypatch):
    """A clean 500 is only actionable if the caller can quote an id for it.

    Starlette installs ``ServerErrorMiddleware`` outside the user middleware
    stack, so a raising route unwinds past ``RequestIDMiddleware`` before it can
    decorate anything — the 500 used to go out with no id while the log line had
    one, which is the worst of both.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", True)

    with exploding_client() as client:
        response = client.get("/__boom", headers={"x-request-id": "abc123"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "abc123"


def test_an_unhandled_500_still_gets_an_id_the_caller_never_supplied(
    exploding_client, monkeypatch
):
    """No inbound header: the generated id must still come back, not "unknown"."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", False)

    with exploding_client() as client:
        response = client.get("/__boom")

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert request_id != "unknown"
    assert len(request_id) == 12
    int(request_id, 16)  # the middleware's uuid4().hex[:12]


def test_a_successful_response_keeps_its_id_too(monkeypatch):
    """The header is added in two places now; neither may shadow the other."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", False)

    with TestClient(create_app()) as client:
        response = client.get("/", headers={"x-request-id": "def456"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "def456"
