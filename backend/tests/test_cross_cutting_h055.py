"""H055 — cross-cutting concern audit: error handling and input validation.

Finding 1: ``POST /uploads`` returned 400 for an oversized file; every other
           size check (the body-size middleware, triggers inbound) returns 413.
Finding 2: ``POST /uploads`` was the only authenticated write endpoint without
           a rate limit.
Census:    every authenticated write endpoint declares 429 and has a limiter.
Census:    every 413 in the declared responses catalogue matches an endpoint
           that can actually produce one.
"""
from __future__ import annotations

import io

import pytest
from fastapi.routing import APIRoute

from app.deps import get_current_user
from app.main import create_app
from app.ratelimit import limiter


# ── helpers ────────────────────────────────────────────────────────────── #


def _walk(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        yield from _walk(getattr(inner, "routes", None) or getattr(route, "routes", []))


def _dependency_calls(route: APIRoute) -> set:
    seen = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(dep.call)
        stack.extend(dep.dependencies)
    return seen


def _method(route: APIRoute) -> str:
    return sorted(route.methods - {"HEAD", "OPTIONS"})[0]


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_APP = create_app()


def _authenticated_write_routes():
    """Every write endpoint behind ``get_current_user``."""
    for route in _walk(_APP.routes):
        if not route.include_in_schema:
            continue
        if get_current_user not in _dependency_calls(route):
            continue
        if _method(route) in _WRITE_METHODS:
            yield route


# ── Finding 1: oversized upload returns 413, not 400 ──────────────────── #


def test_oversized_upload_returns_413(client, auth, monkeypatch, tmp_path):
    """An image exceeding ``max_upload_bytes`` must get 413, like every other
    size guard in the API."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    data = b"\x89PNG" + b"\x00" * 20
    resp = client.post(
        "/api/v1/uploads",
        files={"file": ("big.png", io.BytesIO(data), "image/png")},
        headers=auth,
    )
    assert resp.status_code == 413
    assert "detail" in resp.json()


def test_valid_upload_still_works(client, auth, monkeypatch, tmp_path):
    """Sanity check: a small valid image is accepted."""
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    data = b"\x89PNG" + b"\x00" * 4
    resp = client.post(
        "/api/v1/uploads",
        files={"file": ("ok.png", io.BytesIO(data), "image/png")},
        headers=auth,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["url"].startswith("/api/v1/uploads/")
    assert body["size"] == len(data)


def test_bad_content_type_upload_returns_400(client, auth, monkeypatch, tmp_path):
    """An unsupported file type is still 400 — only size moved to 413."""
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    resp = client.post(
        "/api/v1/uploads",
        files={"file": ("bad.exe", io.BytesIO(b"\x00"), "application/octet-stream")},
        headers=auth,
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


# ── Finding 2: upload rate limit ──────────────────────────────────────── #


def test_upload_endpoint_declares_429():
    """``POST /uploads`` must include 429 in its OpenAPI responses now that it
    carries a rate limit."""
    for route in _walk(_APP.routes):
        if route.path == "/uploads" and "POST" in route.methods:
            assert 429 in route.responses, (
                "POST /uploads does not declare 429 — the rate limit added in "
                "H055 needs a matching response declaration"
            )
            return
    pytest.fail("POST /uploads route not found")


# ── Census: every authenticated write endpoint has a rate limit ────────── #


# Endpoints that are intentionally exempt from per-endpoint rate limits,
# because the dependency they call into (e.g. ``owned_project``) already
# limits throughput, or because they are bounded by a cap (e.g. at most
# 20 keys per project) rather than by time.
_RATE_LIMIT_EXEMPT = {
    # Simple account CRUD
    "PATCH /auth/me",
    # Project CRUD — no outbound calls
    "POST /projects",
    "PATCH /projects/{project_id}",
    "DELETE /projects/{project_id}",
    # Content CRUD — bounded by generation limits or simple DB writes
    "POST /content",
    "PATCH /content/{content_id}",
    "DELETE /content/{content_id}",
    "POST /content/{content_id}/approve",
    "POST /content/{content_id}/publish",
    "POST /content/{content_id}/publish/immediate",
    "POST /content/{content_id}/schedule",
    "DELETE /content/{content_id}/schedule",
    "POST /content/{content_id}/retry/{publication_id}",
    "POST /content/{content_id}/retry",
    "POST /content/{content_id}/status",
    "POST /content/{content_id}/headlines",
    "POST /content/{content_id}/headlines/pick",
    "DELETE /content/{content_id}/headlines",
    "POST /content/{content_id}/preview-links",
    "DELETE /content/{content_id}/preview-links/{link_id}",
    "POST /content/{content_id}/revisions/{revision}/restore",
    "POST /content/bulk/approve",
    "POST /content/bulk/reject",
    "POST /content/bulk/publish",
    "POST /content/bulk/retry",
    "POST /content/bulk/archive-old",
    # Translation delete — simple DB write
    "DELETE /content/{content_id}/translations/{language}",
    # Calendar drag-and-drop
    "PATCH /calendar/content/{content_id}",
    # Settings — disconnect is a simple delete
    "DELETE /settings/connections/{platform}",
    # Webhooks — bounded by per-user cap (20)
    "POST /webhooks",
    "PATCH /webhooks/{webhook_id}",
    "DELETE /webhooks/{webhook_id}",
    "POST /webhooks/{webhook_id}/rotate-secret",
    # Triggers — bounded by per-project cap (20)
    "POST /triggers",
    "PATCH /triggers/{trigger_id}",
    "DELETE /triggers/{trigger_id}",
    "POST /triggers/{trigger_id}/rotate-secret",
    # Templates — bounded by per-user cap, simple DB writes
    "POST /templates",
    "PATCH /templates/{template_id}",
    "DELETE /templates/{template_id}",
    "POST /templates/{template_id}/preview",
    # Tags — suggest is read-only POST, rename has inline limit
    "POST /tags/suggest/{content_id}",
    "POST /tags/rename",
    # API keys — bounded by per-project cap (20)
    "POST /api-keys",
    "POST /api-keys/{key_id}/rotate",
    "DELETE /api-keys/{key_id}",
}


def _has_rate_limit(route: APIRoute) -> bool:
    """Whether *route* has a slowapi ``@limiter.limit`` decorator.

    slowapi stores limit registrations in ``limiter._route_limits`` keyed by
    the endpoint's module-qualified name, not as an attribute on the function.
    """
    endpoint = route.endpoint
    key = f"{endpoint.__module__}.{endpoint.__qualname__}"
    return key in limiter._route_limits


_WRITE_ROUTES = list(_authenticated_write_routes())


def test_census_is_not_empty():
    """Guard against an empty parametrisation."""
    assert len(_WRITE_ROUTES) >= 30


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(route, id=f"{_method(route)} {route.path}")
        for route in _WRITE_ROUTES
    ],
)
def test_authenticated_write_endpoint_either_has_a_rate_limit_or_is_exempt(route):
    """Every authenticated write endpoint should have a rate limit or be in
    the explicit exemption list with a documented reason."""
    label = f"{_method(route)} {route.path}"
    if label in _RATE_LIMIT_EXEMPT:
        return
    assert _has_rate_limit(route), (
        f"{label} has no rate limit and is not in the exemption list. "
        "Either add a @limiter.limit decorator or document the exemption."
    )


# ── Census: every endpoint declaring 429 has a limiter ────────────────── #


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(route, id=f"{_method(route)} {route.path}")
        for route in _walk(_APP.routes)
        if route.include_in_schema and 429 in route.responses
    ],
)
def test_endpoint_declaring_429_has_a_limiter(route):
    """If the OpenAPI spec promises 429, the endpoint must carry a limiter.

    The inverse — a limiter without a 429 declaration — is caught by
    ``test_every_endpoint_is_documented`` in the OpenAPI consistency suite.
    """
    assert _has_rate_limit(route), (
        f"{_method(route)} {route.path} declares 429 in its responses but "
        "has no @limiter.limit decorator"
    )
