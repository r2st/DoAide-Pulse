"""H063 — observability, M11 pass.

Four findings:

1. ``routers/viral.py`` swallowed ``IntegrityError`` on duplicate subscriber
   with no log line, hiding repeated subscribe attempts.
2. ``services/triggers.py`` silently fell back to defaults when
   ``every_hours`` or ``hour_utc`` contained unparseable values.
3. ``routers/viral.py`` logged Gemini parse failures without the response
   body, making diagnosis impossible without reproducing the call.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import triggers


# ── Helpers ─────────────────────────────────────────────────────────── #


class _FakeTrigger:
    """Minimal stand-in for Trigger with a config dict."""

    def __init__(self, *, id: int = 1, config: dict | None = None):
        self.id = id
        self.config = config or {}

    def setting(self, key: str, default=None):
        return (self.config or {}).get(key, default)


# ── Finding 1: duplicate subscriber IntegrityError is logged ─────── #


class TestDuplicateSubscriberLogged:
    """A duplicate subscriber must produce a DEBUG log, not silence."""

    def test_duplicate_subscriber_logs_email(self, caplog, monkeypatch):
        from sqlalchemy.exc import IntegrityError
        from app.routers import viral

        class _FakeDB:
            def add(self, obj):
                pass

            def commit(self):
                raise IntegrityError("dup", {}, Exception("unique"))

            def rollback(self):
                pass

        fake_payload = SimpleNamespace(email="Dup@Example.COM", source="embed")

        with caplog.at_level(logging.DEBUG, logger="app.routers.viral"):
            result = viral.subscribe.__wrapped__(
                request=None,
                response=None,
                payload=fake_payload,
                db=_FakeDB(),
            )

        assert result.ok is True
        assert "dup@example.com" in caplog.text


# ── Finding 2: unparseable every_hours is logged ─────────────────── #


class TestUnparseableEveryHoursLogged:
    """A junk ``every_hours`` must log a warning, not silently default."""

    def test_warning_on_junk_every_hours(self, caplog):
        trigger = _FakeTrigger(id=99, config={"every_hours": "not-a-number"})
        with caplog.at_level(logging.WARNING, logger="app.services.triggers"):
            result = triggers.interval_hours(trigger)

        assert result > 0
        assert "99" in caplog.text
        assert "not-a-number" in caplog.text

    def test_no_warning_on_valid_every_hours(self, caplog):
        trigger = _FakeTrigger(id=99, config={"every_hours": "6"})
        with caplog.at_level(logging.WARNING, logger="app.services.triggers"):
            result = triggers.interval_hours(trigger)

        assert result == 6.0
        assert caplog.text == ""


# ── Finding 3: unparseable hour_utc is logged ────────────────────── #


class TestUnparseableHourUtcLogged:
    """A junk ``hour_utc`` must log a warning, not silently ignore."""

    def test_warning_on_junk_hour_utc(self, caplog, monkeypatch):
        from app.models.trigger import TriggerKind

        trigger = _FakeTrigger(id=77, config={"hour_utc": "abc"})
        trigger.kind = TriggerKind.SCHEDULE
        trigger.last_fired_at = None

        with caplog.at_level(logging.WARNING, logger="app.services.triggers"):
            triggers.is_due(trigger)

        assert "77" in caplog.text
        assert "abc" in caplog.text

    def test_no_warning_on_valid_hour_utc(self, caplog):
        from app.models.trigger import TriggerKind

        trigger = _FakeTrigger(id=77, config={"hour_utc": "14"})
        trigger.kind = TriggerKind.SCHEDULE
        trigger.last_fired_at = None

        with caplog.at_level(logging.WARNING, logger="app.services.triggers"):
            triggers.is_due(trigger)

        assert "unparseable" not in caplog.text


# ── Finding 4: Gemini parse failure includes response body ────────── #


class TestGeminiParseFailureIncludesBody:
    """A Gemini parse failure must include the response text for debugging."""

    def test_parse_failure_includes_response_body(self, caplog, monkeypatch):
        import httpx
        from app.routers import viral
        from app.config import settings

        monkeypatch.setattr(settings, "gemini_api_key", "test-key")

        bad_body = '{"choices": [{"message": {"content": "not json array"}}]}'
        fake_response = httpx.Response(
            200,
            json=json.loads(bad_body),
            request=httpx.Request("POST", "https://fake.test/chat/completions"),
        )
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: fake_response)

        fake_payload = SimpleNamespace(niche="testing", count=3)

        with caplog.at_level(logging.ERROR, logger="app.routers.viral"):
            with pytest.raises(Exception):
                viral.generate_content_ideas.__wrapped__(
                    request=None,
                    response=None,
                    payload=fake_payload,
                )

        assert "not json array" in caplog.text
