"""H028 — observability, M11 pass 5.

Two findings:

1. ``mailer.send`` logged SMTP failures without the recipient or subject,
   making it impossible to correlate a failed delivery with a user.
2. ``publishing_service._mark_connection_invalid`` silently changed a
   connection's status to INVALID without any log line, hiding a state
   change that blocks all future publishing for that user+platform.
"""
from __future__ import annotations

import logging
import smtplib

import pytest

from app.config import settings
from app.models.platform_connection import ConnectionStatus
from app.models.publication import Platform
from app.services import mailer, publishing_service
from app.services.errors import clip_error


# ── Finding 1: mailer.send log lines include recipient and subject ── #


class _FakeSMTP:
    """Minimal SMTP double that can be told to explode on send."""

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.message = message


class _ExplodingSMTP(_FakeSMTP):
    def send_message(self, message):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")


@pytest.fixture
def _smtp_up(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(settings, "smtp_starttls", False)


@pytest.fixture
def _smtp_broken(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _ExplodingSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(settings, "smtp_starttls", False)


class TestMailerSuccessLogIncludesRecipient:
    """A successful send must log both the subject and the recipient."""

    @pytest.mark.usefixtures("_smtp_up")
    def test_success_log_contains_recipient(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.services.mailer"):
            mailer.send(to="alice@example.com", subject="Weekly digest", body="hi")

        assert "alice@example.com" in caplog.text

    @pytest.mark.usefixtures("_smtp_up")
    def test_success_log_contains_subject(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.services.mailer"):
            mailer.send(to="alice@example.com", subject="Weekly digest", body="hi")

        assert "Weekly digest" in caplog.text


class TestMailerFailureLogIncludesContext:
    """An SMTP failure must log enough to identify which email it was."""

    @pytest.mark.usefixtures("_smtp_broken")
    def test_failure_log_contains_recipient(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.services.mailer"):
            mailer.send(to="bob@example.com", subject="Password reset", body="link")

        assert "bob@example.com" in caplog.text

    @pytest.mark.usefixtures("_smtp_broken")
    def test_failure_log_contains_subject(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.services.mailer"):
            mailer.send(to="bob@example.com", subject="Password reset", body="link")

        assert "Password reset" in caplog.text

    @pytest.mark.usefixtures("_smtp_broken")
    def test_failure_log_contains_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.services.mailer"):
            mailer.send(to="bob@example.com", subject="s", body="b")

        assert "bad credentials" in caplog.text


# ── Finding 2: _mark_connection_invalid logs the transition ───────── #


class _FakeConnection:
    """Minimal stand-in for PlatformConnection."""

    def __init__(self, *, id: int, user_id: int, platform: Platform):
        self.id = id
        self.user_id = user_id
        self.platform = platform
        self.status = "connected"
        self.last_error = None


class TestMarkConnectionInvalidLogs:
    """Invalidating a connection must leave a WARNING in the journal."""

    def test_log_contains_platform(self, caplog, monkeypatch):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        monkeypatch.setattr(
            publishing_service,
            "_connection_for",
            lambda db, uid, plat: conn,
        )
        with caplog.at_level(logging.WARNING, logger="app.services.publishing_service"):
            publishing_service._mark_connection_invalid(
                None, 7, Platform.DEVTO, "Token rejected", connection=conn,
            )

        assert "devto" in caplog.text

    def test_log_contains_user_id(self, caplog, monkeypatch):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        with caplog.at_level(logging.WARNING, logger="app.services.publishing_service"):
            publishing_service._mark_connection_invalid(
                None, 7, Platform.DEVTO, "Token rejected", connection=conn,
            )

        assert "7" in caplog.text

    def test_log_contains_connection_id(self, caplog, monkeypatch):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        with caplog.at_level(logging.WARNING, logger="app.services.publishing_service"):
            publishing_service._mark_connection_invalid(
                None, 7, Platform.DEVTO, "Token rejected", connection=conn,
            )

        assert "42" in caplog.text

    def test_log_contains_error_reason(self, caplog, monkeypatch):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        with caplog.at_level(logging.WARNING, logger="app.services.publishing_service"):
            publishing_service._mark_connection_invalid(
                None, 7, Platform.DEVTO, "Token rejected by platform", connection=conn,
            )

        assert "Token rejected" in caplog.text

    def test_status_is_set_to_invalid(self):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        publishing_service._mark_connection_invalid(
            None, 7, Platform.DEVTO, "Expired", connection=conn,
        )

        assert conn.status is ConnectionStatus.INVALID

    def test_last_error_is_set(self):
        conn = _FakeConnection(id=42, user_id=7, platform=Platform.DEVTO)
        publishing_service._mark_connection_invalid(
            None, 7, Platform.DEVTO, "Expired token", connection=conn,
        )

        assert conn.last_error == clip_error("Expired token")
