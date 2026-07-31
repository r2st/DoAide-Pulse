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


def test_custom_headers_exposed_via_cors(client):
    """X-Total-Count and X-Request-ID must be readable by the browser.

    ``expose_headers`` is sent on *actual* responses (not the preflight
    OPTIONS), so we check a simple GET with an Origin header.
    """
    resp = client.get("/api/v1/health", headers={"Origin": ALLOWED})
    exposed = resp.headers.get("access-control-expose-headers", "")
    for header in ("X-Total-Count", "X-Request-ID"):
        assert header.lower() in exposed.lower(), f"{header} not exposed via CORS"


def test_security_headers_present(client):
    """Every response should carry defence-in-depth security headers."""
    resp = client.get("/api/v1/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "strict-origin" in resp.headers["referrer-policy"]
    assert "camera=()" in resp.headers["permissions-policy"]


def test_request_id_header_returned(client):
    """Every response should include X-Request-ID for log correlation."""
    resp = client.get("/api/v1/health")
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 0


def test_request_id_echoed_when_provided(client):
    """A client-supplied X-Request-ID should be echoed back."""
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "test-123"})
    assert resp.headers["x-request-id"] == "test-123"


def test_oversized_body_rejected(client):
    """Bodies larger than 1 MB should be rejected with 413."""
    resp = client.post(
        "/api/v1/auth/register",
        content="x" * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json", "Content-Length": str(1024 * 1024 + 1)},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_malformed_content_length_rejected(client):
    """A non-numeric Content-Length must return 400, not crash."""
    resp = client.post(
        "/api/v1/auth/register",
        content="{}",
        headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower()


def test_negative_content_length_rejected(client):
    """Negative Content-Length must return 400."""
    resp = client.post(
        "/api/v1/auth/register",
        content="{}",
        headers={"Content-Type": "application/json", "Content-Length": "-1"},
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower()
