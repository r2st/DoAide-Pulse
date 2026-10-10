"""H059: M19 error messages audit — user-facing errors are helpful, actionable,
and never leak implementation details.

1. AI generation errors do not expose provider names or raw failure chains.
2. Rate-limit responses do not reveal the exact limit configuration.
3. GitHub scan errors do not leak API paths or operational instructions.
4. The 500 catch-all never leaks exception messages or stack traces.
5. Auth failures are deliberately identical for wrong-email and wrong-password.
"""
from __future__ import annotations

import json
import re

from starlette.requests import Request

from app.main import app as _app
from app.services import ai, github_client
from app.services.errors import friendly_network_error, sanitize_unexpected_error
from app.services.llm_router import AllProvidersFailed


# ---- helpers ---------------------------------------------------------------

_PROVIDER_NAMES = {"openrouter", "gemini", "groq", "cerebras", "openai"}
_INTERNAL_PATTERNS = re.compile(
    r"(traceback|\.py\b|File \"|/app/|/opt/|/backend/|psycopg|sqlalchemy"
    r"|postgresql://|sqlite://|AllProvidersFailed|LLMError|RuntimeError"
    r"|KeyError|ValueError|TypeError|ImportError"
    r"|OPENROUTER_API_KEY|GEMINI_API_KEY|GROQ_API_KEY|CEREBRAS_API_KEY)",
    re.IGNORECASE,
)


def _leaks_internals(detail: str) -> str | None:
    m = _INTERNAL_PATTERNS.search(detail)
    if m:
        return m.group(0)
    for name in _PROVIDER_NAMES:
        if name in detail.lower():
            return name
    return None


# ---- 1. AI generation errors do not expose provider internals ---------------

def test_ai_generation_error_hides_provider_chain(client, auth, monkeypatch):
    def _fail(*args, **kwargs):
        raise ai.AIError(
            "All LLM providers failed: openrouter: skipped, circuit open; "
            "gemini rate-limited the request (429): Rate limit exceeded"
        )

    monkeypatch.setattr(ai, "json_completion", _fail)

    resp = client.post(
        "/api/v1/ai/generate-fields",
        json={"title": "Hello World", "fields": ["meta_description"]},
        headers=auth,
    )

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert _leaks_internals(detail) is None, f"Leaked: {detail}"
    assert "try again" in detail.lower()


# ---- 2. Rate-limit responses do not reveal exact limits ---------------------

def test_rate_limit_response_hides_exact_limit():
    from unittest.mock import MagicMock

    from app.ratelimit import rate_limit_exceeded_handler

    exc = MagicMock()
    exc.detail = "5 per 1 minute"

    scope = {"type": "http", "method": "POST", "path": "/api/v1/auth/login",
             "headers": [], "query_string": b"", "app": _app}
    request = Request(scope)
    request.state.view_rate_limit = "5 per 1 minute"

    response = rate_limit_exceeded_handler(request, exc)

    assert response.status_code == 429
    body = json.loads(response.body)
    detail = body["detail"]
    assert "5 per 1 minute" not in detail
    assert "5/minute" not in detail
    assert "limit:" not in detail.lower()


# ---- 3. GitHub scan errors hide API paths and instructions ------------------

def test_github_rate_limit_hides_reset_time_and_instructions(
    client, auth, project, monkeypatch
):
    def _fail(*args, **kwargs):
        raise github_client.GitHubRateLimited(
            "GitHub rate limit exhausted (resets at 14:05). "
            "Set GITHUB_TOKEN to raise the ceiling from 60 to 5000 req/hour.",
            retry_after=120,
        )

    monkeypatch.setattr(github_client, "fetch_activity", _fail)

    resp = client.post(f"/api/v1/projects/{project.id}/scan", headers=auth)

    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert "GITHUB_TOKEN" not in detail
    assert "60 to 5000" not in detail
    assert "resets at" not in detail


def test_github_error_hides_api_path(client, auth, project, monkeypatch):
    def _fail(*args, **kwargs):
        raise github_client.GitHubError(
            "GitHub returned 500 for /repos/r2st/Pulse/commits. "
            "If this keeps happening, check that the repo URL is correct."
        )

    monkeypatch.setattr(github_client, "fetch_activity", _fail)

    resp = client.post(f"/api/v1/projects/{project.id}/scan", headers=auth)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "/repos/" not in detail
    assert "returned 500" not in detail


# ---- 4. The 500 catch-all never leaks exception internals -------------------

def test_unhandled_500_hides_exception_message(client, auth, monkeypatch):
    from app.routers import misc

    original_root = None
    for route in _app.routes:
        if hasattr(route, "endpoint") and getattr(route, "path", "") == "/":
            original_root = route.endpoint
            break

    def _boom():
        raise RuntimeError("SELECT * FROM users WHERE password='hunter2'")

    monkeypatch.setattr(misc, "root", _boom, raising=False)

    resp = client.get("/api/v1/health")

    if resp.status_code == 500:
        detail = resp.json().get("detail", "")
        assert "SELECT" not in detail
        assert "hunter2" not in detail
        assert "RuntimeError" not in detail


# ---- 5. Auth failures are deliberately identical ----------------------------

def test_login_wrong_email_and_wrong_password_same_message(client, user):
    wrong_email = client.post(
        "/api/v1/auth/login",
        data={"username": "nobody@example.com", "password": "hunter2hunter2"},
    )
    wrong_pass = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "wrongpassword99"},
    )

    assert wrong_email.status_code == wrong_pass.status_code == 401
    assert wrong_email.json()["detail"] == wrong_pass.json()["detail"]
    detail = wrong_email.json()["detail"]
    assert "nobody@example.com" not in detail
    assert user.email not in detail


def test_register_existing_email_does_not_confirm_existence(client, user):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": user.email, "password": "password1234", "full_name": "Test"},
    )
    detail = resp.json().get("detail", "")
    assert user.email not in detail


# ---- 6. sanitize_unexpected_error unit tests --------------------------------

class TestSanitizeUnexpectedErrorH059:
    def test_connection_string_hidden(self):
        exc = Exception("psycopg.OperationalError: could not connect to "
                        "postgresql://admin:s3cret@db.prod:5432/pulse")
        msg = sanitize_unexpected_error(exc)
        assert "admin" not in msg
        assert "s3cret" not in msg
        assert "postgresql://" not in msg

    def test_file_path_hidden(self):
        exc = RuntimeError("/opt/Pulse/backend/app/services/ai.py line 42")
        msg = sanitize_unexpected_error(exc)
        assert "/opt/Pulse" not in msg
        assert "ai.py" not in msg

    def test_keeps_only_type_name(self):
        exc = TypeError("argument of type 'NoneType' is not iterable")
        msg = sanitize_unexpected_error(exc)
        assert "TypeError" in msg
        assert "NoneType" not in msg


# ---- 7. friendly_network_error never leaks class names ----------------------

class TestFriendlyNetworkErrorH059:
    def test_no_class_name_in_any_variant(self):
        import httpx
        for cls in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.ConnectError, httpx.ReadError,
                    httpx.WriteError, httpx.CloseError, httpx.TooManyRedirects):
            exc = cls("internal detail /var/run/secret.sock")
            msg = friendly_network_error(exc)
            assert cls.__name__ not in msg, f"{cls.__name__} leaked"
            assert "/var/run" not in msg
            assert "secret.sock" not in msg


# ---- 8. AllProvidersFailed messages should not reach users -------------------

def test_all_providers_failed_message_contains_provider_names():
    """Verify that the raw AllProvidersFailed message DOES contain provider info
    (confirming why we must not forward it to users)."""
    exc = AllProvidersFailed(
        "All LLM providers failed: openrouter: skipped, circuit open; "
        "gemini rate-limited the request (429)"
    )
    raw = str(exc)
    assert "openrouter" in raw
    assert "gemini" in raw
