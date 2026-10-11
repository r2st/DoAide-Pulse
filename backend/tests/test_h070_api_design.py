"""H070 M16: API Design audit.

Verifies HTTP status code correctness, REST naming, error response format
consistency, proper use of HTTP methods, and that status codes use symbolic
constants throughout the router layer.
"""
from __future__ import annotations

import importlib
import inspect
import re

import pytest
from fastapi import status
from fastapi.routing import APIRoute

from app.main import create_app


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def routes(app):
    collected = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            collected.append(r)
        elif hasattr(r, "original_router"):
            for sr in r.original_router.routes:
                if isinstance(sr, APIRoute):
                    collected.append(sr)
    return collected


_ROUTER_MODULES = [
    "ai_generate",
    "analytics",
    "api_keys",
    "auth",
    "calendar",
    "content",
    "machine",
    "metrics",
    "misc",
    "projects",
    "revisions",
    "settings",
    "tags",
    "templates",
    "translations",
    "triggers",
    "uploads",
    "viral",
    "webhooks",
]


def _load_router(name: str):
    return importlib.import_module(f"app.routers.{name}")


# ---------------------------------------------------------------------------
# 1. No bare integer status codes — every router uses status.HTTP_* constants
# ---------------------------------------------------------------------------


_BARE_STATUS_RE = re.compile(r"status_code\s*=\s*(\d{3})\b")


class TestNoBareLiteralStatusCodes:
    @pytest.mark.parametrize("module_name", _ROUTER_MODULES)
    def test_no_bare_integer_status_codes(self, module_name):
        """Every status_code= in the router layer should use a status.HTTP_*
        constant, not a bare integer like 201 or 404."""
        mod = _load_router(module_name)
        source = inspect.getsource(mod)
        matches = _BARE_STATUS_RE.findall(source)
        assert not matches, (
            f"{module_name}.py uses bare status code literal(s): "
            + ", ".join(matches)
            + " — use status.HTTP_* constants"
        )


# ---------------------------------------------------------------------------
# 2. Resource-creating POST endpoints return 201
# ---------------------------------------------------------------------------

# Endpoints that create a resource (db.add + commit) should have status_code=201.
# Action/RPC endpoints (approve, retry, publish, rename, etc.) correctly use 200.
_MUST_BE_201 = {
    "register",
    "create_content",
    "generate_content",
    "create_project",
    "create_template",
    "create_trigger",
    "create_webhook",
    "create_api_key",
    "upload_image",
    "create_preview_link",
    "create_translation",
    "subscribe",
    "write_from_idea",
    "machine_idea",
    "clone_template",
}

class TestCreationEndpointsReturn201:
    def test_resource_creators_use_201(self, routes):
        """POST endpoints that create a new resource must return 201."""
        violations = []
        for route in routes:
            if "POST" not in route.methods:
                continue
            if route.name not in _MUST_BE_201:
                continue
            sc = route.status_code or 200
            if sc != status.HTTP_201_CREATED:
                violations.append(f"{route.name}: {route.path} returns {sc}")
        assert not violations, (
            "Resource-creating endpoints should return 201:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 3. DELETE endpoints return 204 when they have no body
# ---------------------------------------------------------------------------


class TestDeleteReturns204OrBody:
    def test_delete_status_codes(self, routes):
        """DELETE endpoints should return 204 (no body) or 200 (with body).
        No DELETE should return the default 200 with response_model=None."""
        violations = []
        for route in routes:
            if "DELETE" not in route.methods:
                continue
            sc = route.status_code or 200
            if route.response_model is None and sc != status.HTTP_204_NO_CONTENT:
                violations.append(
                    f"{route.name}: {route.path} returns {sc} with no "
                    f"response_model — should be 204"
                )
        assert not violations, (
            "DELETE without a response body should return 204:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 4. Every endpoint has a response_model (except 204 endpoints)
# ---------------------------------------------------------------------------


_NO_MODEL_ALLOWLIST = {
    "project_feed",
    "serve_image",
}


class TestEveryEndpointHasResponseModel:
    def test_non_204_endpoints_have_response_model(self, routes):
        """Every non-204 endpoint should declare a Pydantic response_model."""
        violations = []
        for route in routes:
            sc = route.status_code or 200
            if sc == status.HTTP_204_NO_CONTENT:
                continue
            if route.name in _NO_MODEL_ALLOWLIST:
                continue
            if route.response_model is None:
                violations.append(
                    f"{list(route.methods)} {route.path} ({route.name})"
                )
        assert not violations, (
            "These endpoints have no response_model:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 5. Error responses all use ErrorOut shape
# ---------------------------------------------------------------------------


_ERROR_MODEL_ALLOWLIST = {
    "health",
    "health_detail",
}


class TestErrorResponseFormat:
    def test_all_error_responses_use_error_out(self, routes):
        """Every error response declared in ``responses=`` should use ErrorOut
        (the ``{"detail": "..."}`` shape), except 422 which FastAPI generates
        and health endpoints which return their own status shape on 503."""
        from app.schemas.errors import ErrorOut

        violations = []
        for route in routes:
            if route.name in _ERROR_MODEL_ALLOWLIST:
                continue
            for code, spec in (route.responses or {}).items():
                if isinstance(code, str):
                    continue
                if 200 <= code < 300:
                    continue
                if code == 422:
                    continue
                model = spec.get("model")
                if model is not None and model is not ErrorOut:
                    violations.append(
                        f"{route.path} status {code}: model={model.__name__}, "
                        f"expected ErrorOut"
                    )
        assert not violations, (
            "Error responses should use ErrorOut:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 6. Subscriber creation returns 201
# ---------------------------------------------------------------------------


class TestSubscriberCreation:
    def test_subscribe_returns_201(self, client):
        resp = client.post(
            "/api/v1/subscribers",
            json={"email": "test-h070@example.com"},
        )
        assert resp.status_code == 201, (
            f"POST /subscribers should return 201 for resource creation, "
            f"got {resp.status_code}"
        )
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# 7. Special status codes are correct
# ---------------------------------------------------------------------------


class TestSpecialStatusCodes:
    def test_password_reset_returns_202(self, routes):
        """POST /password-reset should return 202 (async processing)."""
        for route in routes:
            if route.name == "request_password_reset":
                sc = route.status_code or 200
                assert sc == status.HTTP_202_ACCEPTED, (
                    f"password-reset should return 202, got {sc}"
                )
                return
        pytest.fail("request_password_reset route not found")

    def test_inbound_trigger_returns_202(self, routes):
        """POST /triggers/inbound/{token} should return 202 (async)."""
        for route in routes:
            if route.name == "inbound":
                sc = route.status_code or 200
                assert sc == status.HTTP_202_ACCEPTED, (
                    f"inbound should return 202, got {sc}"
                )
                return
        pytest.fail("inbound route not found")
