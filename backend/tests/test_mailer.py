"""The SMTP mailer.

No mail server is contacted: ``smtplib.SMTP`` is replaced with a recorder, which
is the only way to assert on STARTTLS and login ordering without one.
"""
from __future__ import annotations

import smtplib

import pytest

from app.config import settings
from app.services import mailer


class _FakeSMTP:
    """Records the sequence of calls an SMTP session receives."""

    instances: list[_FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message):
        self.calls.append("send")
        self.message = message


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "pulse@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "smtp_from", "Pulse <pulse@example.com>")
    return _FakeSMTP


def test_not_configured_without_a_host():
    assert mailer.configured() is False
    assert mailer.send(to="a@example.com", subject="s", body="b") is False


def test_starttls_then_login_then_send(smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)

    assert mailer.send(to="dev@example.com", subject="Reset", body="link") is True

    session = smtp.instances[0]
    # Order matters: credentials must not go out before the channel is encrypted.
    assert session.calls == [
        "starttls",
        "login:pulse@example.com:app-password",
        "send",
        "quit",
    ]
    assert session.message["To"] == "dev@example.com"
    assert session.message["From"] == "Pulse <pulse@example.com>"
    assert session.message["Subject"] == "Reset"
    assert session.message.get_content().strip() == "link"


def test_implicit_tls_does_not_also_starttls(smtp, monkeypatch):
    """STARTTLS on an already-encrypted SMTP_SSL connection is an error."""
    monkeypatch.setattr(settings, "smtp_use_ssl", True)
    monkeypatch.setattr(settings, "smtp_starttls", True)

    assert mailer.send(to="dev@example.com", subject="s", body="b") is True
    assert "starttls" not in smtp.instances[0].calls


def test_anonymous_relay_skips_login(smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_user", "")
    mailer.send(to="dev@example.com", subject="s", body="b")
    assert not any(c.startswith("login") for c in smtp.instances[0].calls)


@pytest.mark.parametrize(
    "error", [smtplib.SMTPAuthenticationError(535, b"nope"), OSError("connection refused")]
)
def test_send_failures_are_reported_not_raised(smtp, monkeypatch, error):
    """The caller answers the same either way, so a raise would only be a 500."""
    def explode(self, message):
        raise error

    monkeypatch.setattr(_FakeSMTP, "send_message", explode)
    assert mailer.send(to="dev@example.com", subject="s", body="b") is False


def test_from_address_falls_back_to_the_host(smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_from", "")
    mailer.send(to="dev@example.com", subject="s", body="b")
    assert smtp.instances[0].message["From"] == "pulse@smtp.example.com"
