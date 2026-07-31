"""Settings / platform connection endpoint tests.

The settings page lists platforms, lets users connect credentials, re-verify
them, and disconnect. These tests exercise the router paths without actually
calling any platform API — the adapter is stubbed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services.publishers.base import CredentialError, CredentialField


def test_list_platforms(client, auth):
    resp = client.get("/api/v1/settings/platforms", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) > 0
    for entry in body:
        assert "platform" in entry
        assert "display_name" in entry
        assert "implemented" in entry


def test_list_platforms_requires_auth(client):
    assert client.get("/api/v1/settings/platforms").status_code == 401


def test_delete_nonexistent_connection_is_404(client, auth):
    resp = client.delete("/api/v1/settings/connections/devto", headers=auth)
    assert resp.status_code == 404


def test_verify_nonexistent_connection_is_404(client, auth):
    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)
    assert resp.status_code == 404


def test_connect_and_disconnect(client, auth, db, user, monkeypatch):
    """Full lifecycle: connect → list → disconnect."""
    from app.services import publishers

    # Stub the adapter so no real API call is made
    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(adapter, "verify", lambda creds: "test-user")
    monkeypatch.setattr(
        adapter, "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    # Connect
    resp = client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={"platform": "devto", "credentials": {"api_key": "fake-key"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "devto"
    assert body["status"] == "connected"
    assert body["display_name"] == "test-user"

    # Appears in platform list
    platforms = client.get("/api/v1/settings/platforms", headers=auth).json()
    devto = next(p for p in platforms if p["platform"] == "devto")
    assert devto["connection"] is not None
    assert devto["connection"]["status"] == "connected"

    # Disconnect
    resp = client.delete("/api/v1/settings/connections/devto", headers=auth)
    assert resp.status_code == 204

    # Gone
    platforms = client.get("/api/v1/settings/platforms", headers=auth).json()
    devto = next(p for p in platforms if p["platform"] == "devto")
    assert devto["connection"] is None


def test_connect_missing_required_field(client, auth, monkeypatch):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter, "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    resp = client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={"platform": "devto", "credentials": {"api_key": ""}},
    )
    assert resp.status_code == 400
    assert "api_key" in resp.json()["detail"]


def test_connect_unknown_field(client, auth, monkeypatch):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter, "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    resp = client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={
            "platform": "devto",
            "credentials": {"api_key": "ok", "bogus": "nope"},
        },
    )
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_connect_bad_credentials(client, auth, monkeypatch):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter, "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )
    monkeypatch.setattr(
        adapter, "verify",
        lambda creds: (_ for _ in ()).throw(CredentialError("Invalid API key")),
    )

    resp = client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={"platform": "devto", "credentials": {"api_key": "bad"}},
    )
    assert resp.status_code == 400
    assert "Invalid" in resp.json()["detail"]
