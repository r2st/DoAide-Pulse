"""CORS policy.

Herald authenticates with a bearer token the SPA sends explicitly. Nothing is
carried in a cookie, so credentialed cross-origin requests are neither needed
nor allowed.

In production there is no cross-origin request at all — Caddy serves the SPA and
the API from one origin — so this policy only governs the dev server on :5173
and anything else the operator adds to BACKEND_CORS_ORIGINS.
"""
from __future__ import annotations

from app.config import settings

ALLOWED = "http://localhost:5173"
PREFLIGHT_HEADERS = {
    "Origin": ALLOWED,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


def test_allowed_origin_is_echoed_without_credentials(client):
    resp = client.options("/api/v1/auth/login", headers=PREFLIGHT_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    # The header a browser needs to see before it will attach cookies or send a
    # credentialed XHR. Its absence is the fix.
    assert "access-control-allow-credentials" not in resp.headers


def test_simple_request_response_carries_no_credentials_header(client):
    resp = client.get("/api/v1/health", headers={"Origin": ALLOWED})
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in resp.headers


def test_authorization_header_is_still_allowed(client):
    """Removing credentials must not break the way Herald actually authenticates."""
    resp = client.options("/api/v1/auth/login", headers=PREFLIGHT_HEADERS)
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed


def test_an_unlisted_origin_is_not_allowed(client):
    resp = client.options(
        "/api/v1/auth/login",
        headers={**PREFLIGHT_HEADERS, "Origin": "https://evil.example"},
    )
    assert "access-control-allow-origin" not in resp.headers


def test_configured_origins_are_an_explicit_list(client):
    """A wildcard plus credentials is the combination browsers reject outright;
    keep the list explicit so nobody is tempted to reintroduce either."""
    assert "*" not in settings.cors_origins
    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:3000"]
