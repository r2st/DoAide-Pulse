"""/docs, /redoc and /openapi.json are development-only.

The schema is a complete inventory of the API and it is served from the same
origin as the SPA, so in production it is removed rather than protected — a 404,
not an auth prompt.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app, docs_enabled

DOC_PATHS = ["/docs", "/redoc", "/openapi.json"]


@pytest.fixture
def app_client():
    """A client over a *freshly built* app, so the gate is re-evaluated."""

    def build():
        return TestClient(create_app())

    return build


@pytest.mark.parametrize("environment", ["production", "PROD"])
def test_docs_are_gone_in_production(app_client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "debug", False)
    assert docs_enabled() is False

    with app_client() as client:
        for path in DOC_PATHS:
            assert client.get(path).status_code == 404, path
        # The API still works — only the schema is withheld.
        assert client.get("/").status_code == 200


def test_root_does_not_advertise_docs_it_does_not_serve(app_client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", False)
    with app_client() as client:
        assert "docs" not in client.get("/").json()


def test_debug_reopens_the_docs_in_production(app_client, monkeypatch):
    """The documented escape hatch for poking at a live box."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", True)
    assert docs_enabled() is True

    with app_client() as client:
        for path in DOC_PATHS:
            assert client.get(path).status_code == 200, path


def test_docs_are_available_in_development(app_client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "debug", False)
    assert docs_enabled() is True

    with app_client() as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/").json()["docs"] == "/docs"
