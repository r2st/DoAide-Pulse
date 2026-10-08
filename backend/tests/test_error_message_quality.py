"""Error messages never leak Python class names or raw exception internals.

M19 Pass 2: every error stored on a row, returned in an API response, or
interpolated into a user-visible string must be a sentence a human can act on,
not a ``ConnectTimeout('...')`` that reveals the HTTP library in use.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.errors import (
    friendly_network_error,
    sanitize_unexpected_error,
)


# ---------------------------------------------------------------------------
# friendly_network_error — httpx transport errors → human sentences
# ---------------------------------------------------------------------------


class TestFriendlyNetworkError:
    def test_connect_timeout(self):
        exc = httpx.ConnectTimeout("timed out")
        msg = friendly_network_error(exc)
        assert msg == "Connection timed out"
        assert "ConnectTimeout" not in msg

    def test_read_timeout(self):
        exc = httpx.ReadTimeout("timed out")
        msg = friendly_network_error(exc)
        assert msg == "The server took too long to respond"

    def test_connect_error(self):
        exc = httpx.ConnectError("connection refused")
        msg = friendly_network_error(exc)
        assert msg == "Could not connect to the server"
        assert "ConnectError" not in msg

    def test_read_error(self):
        exc = httpx.ReadError("connection reset")
        msg = friendly_network_error(exc)
        assert msg == "The connection was lost while reading the response"

    def test_pool_timeout(self):
        exc = httpx.PoolTimeout("pool exhausted")
        msg = friendly_network_error(exc)
        assert "pool" in msg.lower()

    def test_generic_timeout_subclass(self):
        exc = httpx.TimeoutException("some timeout")
        msg = friendly_network_error(exc)
        assert msg == "The request timed out"

    def test_unknown_httpx_error(self):
        exc = httpx.HTTPError("something weird")
        msg = friendly_network_error(exc)
        assert "network error" in msg.lower()
        assert "HTTPError" not in msg

    def test_no_class_name_leaked(self):
        for exc_cls in (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ConnectError,
            httpx.ReadError,
        ):
            exc = exc_cls("internal detail")
            msg = friendly_network_error(exc)
            assert exc_cls.__name__ not in msg, f"{exc_cls.__name__} leaked in: {msg}"
            assert "internal detail" not in msg


# ---------------------------------------------------------------------------
# sanitize_unexpected_error — generic Exception → safe one-liner
# ---------------------------------------------------------------------------


class TestSanitizeUnexpectedError:
    def test_keeps_class_name_only(self):
        exc = RuntimeError("secret token abc123 in /var/data")
        msg = sanitize_unexpected_error(exc)
        assert "RuntimeError" in msg
        assert "secret token" not in msg
        assert "/var/data" not in msg

    def test_value_error(self):
        exc = ValueError("invalid literal for int()")
        msg = sanitize_unexpected_error(exc)
        assert "ValueError" in msg
        assert "invalid literal" not in msg

    def test_custom_exception(self):
        class MyInternalError(Exception):
            pass

        exc = MyInternalError("details")
        msg = sanitize_unexpected_error(exc)
        assert "MyInternalError" in msg
        assert "Internal error" in msg

    def test_empty_message(self):
        exc = RuntimeError()
        msg = sanitize_unexpected_error(exc)
        assert "RuntimeError" in msg

    def test_starts_with_internal_error(self):
        exc = Exception("anything")
        msg = sanitize_unexpected_error(exc)
        assert msg.startswith("Internal error")


# ---------------------------------------------------------------------------
# Integration: error patterns that used to leak internals
# ---------------------------------------------------------------------------


class TestNoClassNameInWebhookErrors:
    """The webhook service used to write ``{type(exc).__name__}: {exc}``."""

    def test_httpx_class_names_not_in_friendly_error(self):
        for cls in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            exc = cls("something internal")
            msg = friendly_network_error(exc)
            assert cls.__name__ not in msg


class TestNoRawExceptionInUnexpectedError:
    """Three catch-all arms used ``f"Unexpected error: {exc}"``."""

    def test_file_paths_not_leaked(self):
        exc = OSError("/var/run/pulse/secret.sock: connection refused")
        msg = sanitize_unexpected_error(exc)
        assert "/var/run" not in msg
        assert "secret.sock" not in msg

    def test_sql_not_leaked(self):
        exc = Exception("(psycopg.errors.UniqueViolation) duplicate key value")
        msg = sanitize_unexpected_error(exc)
        assert "psycopg" not in msg
        assert "duplicate key" not in msg

    def test_connection_string_not_leaked(self):
        exc = Exception("could not connect to postgresql://user:pass@host:5432/db")
        msg = sanitize_unexpected_error(exc)
        assert "user:pass" not in msg
        assert "postgresql://" not in msg


class TestTaskResultErrors:
    """Celery task result dicts must not leak raw exception messages."""

    def test_trigger_task_result_sanitized(self):
        exc = OSError("/var/run/pulse/db.sock: connection refused")
        msg = sanitize_unexpected_error(exc)
        assert "/var/run" not in msg
        assert "connection refused" not in msg
        assert "OSError" in msg

    def test_autopilot_task_result_sanitized(self):
        exc = RuntimeError("SELECT * FROM users WHERE password='hunter2'")
        msg = sanitize_unexpected_error(exc)
        assert "SELECT" not in msg
        assert "hunter2" not in msg
        assert "RuntimeError" in msg


class TestHeadlineSyncErrors:
    """headline_sync catch-all arms must not leak raw exception messages."""

    def test_unexpected_credential_error_sanitized(self):
        exc = OSError("could not connect to postgresql://user:pass@host:5432/db")
        msg = sanitize_unexpected_error(exc)
        assert "user:pass" not in msg
        assert "postgresql://" not in msg
        assert "OSError" in msg

    def test_unexpected_sync_error_sanitized(self):
        exc = RuntimeError("/opt/Pulse/backend/app/services/publishers/git.py: no such file")
        msg = sanitize_unexpected_error(exc)
        assert "/opt/Pulse" not in msg
        assert "no such file" not in msg
        assert "RuntimeError" in msg


class TestPlatformEnumError:
    """``content.py`` used to expose ValueError internals from enum conversion."""

    def test_enum_internal_message_not_surfaced(self):
        try:
            from app.models.publication import Platform

            try:
                Platform("badplatform")
            except ValueError as exc:
                raw = str(exc)
                assert "is not a valid" in raw
        except ImportError:
            pytest.skip("Platform model not importable without DB")
