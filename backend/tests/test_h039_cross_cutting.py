"""H039: Cross-cutting concerns — middleware ordering, CORS methods, rate limits.

Four fixes, each pinned by at least one test:

1. **Middleware ordering**: ``RequestIDMiddleware`` now wraps
   ``BodySizeLimitMiddleware``, so body-size rejections carry ``X-Request-ID``
   for log correlation. Before this, a 413 went out with no id, which is the
   one response you most want to look up.

2. **CORS ``allow_methods``**: restricted from ``["*"]`` to the six methods the
   API actually uses. A wildcard would tell a browser that TRACE is fine, and
   while FastAPI answers 405 either way, a preflight that says "yes, TRACE"
   is not the answer this API means to give.

3. **``/health/detail`` rate limit**: the detailed health probe performs a
   Celery worker broadcast with a wait — the most expensive thing a single
   request can do in this codebase — and was the only ``/health`` endpoint
   without a budget.

4. **``/metrics`` rate limit**: ``ops_metrics.build`` aggregates across the
   deployment's usage and publishing tables. One expensive diagnostic read
   per minute is plenty; a loop was unlimited.
"""
from __future__ import annotations

from app.config import settings

ALLOWED = "http://localhost:5173"
PREFLIGHT_HEADERS = {
    "Origin": ALLOWED,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


# ------------------------------------------------------------------ #
# 1. Middleware ordering: body-size rejections carry a request ID     #
# ------------------------------------------------------------------ #


def test_oversized_body_carries_request_id(client):
    """A 413 from the body-size middleware must include X-Request-ID.

    Before the fix, ``BodySizeLimitMiddleware`` sat outside
    ``RequestIDMiddleware``, so rejections bypassed the id assignment and went
    out with no id at all — the exact response an operator most wants to grep
    the logs for.
    """
    resp = client.post(
        "/api/v1/auth/register",
        content="x" * (1024 * 1024 + 1),
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(1024 * 1024 + 1),
        },
    )
    assert resp.status_code == 413
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 0


def test_oversized_body_echoes_client_request_id(client):
    """A client-supplied id should survive a body-size rejection."""
    resp = client.post(
        "/api/v1/auth/register",
        content="x" * (1024 * 1024 + 1),
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(1024 * 1024 + 1),
            "X-Request-ID": "trace-me-413",
        },
    )
    assert resp.status_code == 413
    assert resp.headers["x-request-id"] == "trace-me-413"


def test_deeply_nested_json_rejection_carries_request_id(client):
    """A 400 from the JSON depth scanner must also carry the id."""
    deep = "[" * 40 + "]" * 40
    resp = client.post(
        "/api/v1/auth/register",
        content=deep,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "nested too deeply" in resp.json()["detail"]
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 0


def test_oversized_body_still_gets_security_headers(client):
    """Security headers must be present on body-size rejections too."""
    resp = client.post(
        "/api/v1/auth/register",
        content="x" * (1024 * 1024 + 1),
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(1024 * 1024 + 1),
        },
    )
    assert resp.status_code == 413
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in resp.headers


# ------------------------------------------------------------------ #
# 2. CORS allow_methods is an explicit list, not a wildcard           #
# ------------------------------------------------------------------ #


def test_cors_allows_methods_the_api_uses(client):
    """Every method the API uses should pass preflight."""
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        resp = client.options(
            "/api/v1/auth/login",
            headers={
                **PREFLIGHT_HEADERS,
                "Access-Control-Request-Method": method,
            },
        )
        allowed = resp.headers.get("access-control-allow-methods", "")
        assert method in allowed, f"{method} not in allow-methods: {allowed}"


def test_cors_does_not_allow_trace(client):
    """TRACE must not be advertised as allowed — it is not an API method."""
    resp = client.options(
        "/api/v1/auth/login",
        headers={
            **PREFLIGHT_HEADERS,
            "Access-Control-Request-Method": "TRACE",
        },
    )
    allowed = resp.headers.get("access-control-allow-methods", "")
    assert "TRACE" not in allowed


def test_cors_methods_are_not_a_wildcard(client):
    """The wildcard was replaced with an explicit list."""
    resp = client.options(
        "/api/v1/auth/login",
        headers=PREFLIGHT_HEADERS,
    )
    allowed = resp.headers.get("access-control-allow-methods", "")
    assert allowed != "*", "allow_methods is still a wildcard"


# ------------------------------------------------------------------ #
# 3. /health/detail is rate-limited                                   #
# ------------------------------------------------------------------ #


def test_health_detail_is_rate_limited(client, auth):
    """The detailed health probe must enforce a rate limit."""
    limit = _parse_per_minute(settings.rate_limit_health)
    for _ in range(limit):
        resp = client.get("/api/v1/health/detail", headers=auth)
        assert resp.status_code in (200, 503), resp.text
    resp = client.get("/api/v1/health/detail", headers=auth)
    assert resp.status_code == 429, (
        f"Expected 429 after {limit} requests, got {resp.status_code}"
    )


def test_health_detail_429_has_correct_body(client, auth):
    """The 429 should carry the standard ``detail`` key."""
    limit = _parse_per_minute(settings.rate_limit_health)
    for _ in range(limit):
        client.get("/api/v1/health/detail", headers=auth)
    resp = client.get("/api/v1/health/detail", headers=auth)
    assert resp.status_code == 429
    assert "detail" in resp.json()


# ------------------------------------------------------------------ #
# 4. /metrics is rate-limited                                         #
# ------------------------------------------------------------------ #


def test_metrics_is_rate_limited(client, auth):
    """The metrics endpoint must enforce a rate limit."""
    limit = _parse_per_minute(settings.rate_limit_health)
    for _ in range(limit):
        resp = client.get("/api/v1/metrics", headers=auth)
        assert resp.status_code == 200, resp.text
    resp = client.get("/api/v1/metrics", headers=auth)
    assert resp.status_code == 429, (
        f"Expected 429 after {limit} requests, got {resp.status_code}"
    )


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _parse_per_minute(rate_string: str) -> int:
    """Extract the per-minute count from a rate limit string like '60/minute'.

    Only the first window is read — that is the one slowapi enforces on a
    burst, which is all these tests need.
    """
    first = rate_string.split(";")[0].strip()
    count, _, window = first.partition("/")
    if "minute" in window:
        return int(count)
    if "hour" in window:
        return int(count) * 60
    if "day" in window:
        return int(count) * 60 * 24
    return int(count)
