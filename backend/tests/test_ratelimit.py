"""Rate limiting and the registration gate.

The limits are the real configured ones (``app.config.Settings``): slowapi reads
the limit string when the decorator is applied at import time, so a test cannot
lower them and has to spend the actual budget instead. That keeps these tests
honest about the numbers shipped to production.
"""
from __future__ import annotations

import pytest

from app.config import settings

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


def _login(client, **kwargs):
    return client.post(
        LOGIN, data={"username": "nobody@example.com", "password": "wrongwrongwrong"}, **kwargs
    )


def _payload(email: str = "invitee@example.com", **extra) -> dict:
    return {"email": email, "password": "longenough123", **extra}


# --------------------------------------------------------------------------- #
# Rate limiting                                                               #
# --------------------------------------------------------------------------- #


def test_login_is_rate_limited():
    """LOGIN allows 10/minute; the 11th attempt is refused, not answered 401."""
    assert settings.rate_limit_login.startswith("10/minute")


def test_eleventh_login_attempt_is_429(client):
    for _ in range(10):
        assert _login(client).status_code == 401

    resp = _login(client)
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]
    # A client needs to know how long to wait.
    assert resp.headers.get("retry-after")


def test_register_is_rate_limited(client):
    # 5/hour. Distinct emails so nothing is refused for being a duplicate.
    for i in range(5):
        resp = client.post(REGISTER, json=_payload(f"burst{i}@example.com"))
        assert resp.status_code == 201, resp.text

    resp = client.post(REGISTER, json=_payload("burst5@example.com"))
    assert resp.status_code == 429


def test_rate_limit_buckets_are_per_client_address(client):
    """Two callers behind Caddy must not share one budget."""
    for _ in range(10):
        _login(client, headers={"X-Forwarded-For": "203.0.113.10"})
    assert _login(client, headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 429

    # A different address still has its full allowance.
    assert _login(client, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 401


def test_forwarded_for_cannot_be_spoofed_to_reset_the_budget(client):
    """The rightmost X-Forwarded-For entry is the bucket.

    Caddy appends the peer it saw, so a caller prepending its own values gets
    them ignored — otherwise rate limiting would be one header away from off.
    """
    for _ in range(10):
        _login(client, headers={"X-Forwarded-For": "203.0.113.10"})

    resp = _login(client, headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.10"})
    assert resp.status_code == 429


def test_authenticated_read_is_also_limited():
    assert settings.rate_limit_auth_read == "60/minute"


# --------------------------------------------------------------------------- #
# Registration gate                                                           #
# --------------------------------------------------------------------------- #


def test_registration_is_closed_by_default():
    """The shipped default. conftest opens it for the tests that need it."""
    assert type(settings).model_fields["registration_enabled"].default is False


def test_register_refused_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", False)
    resp = client.post(REGISTER, json=_payload())
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Registration is closed."


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_register_refused_without_the_right_invite_token(client, monkeypatch, token):
    monkeypatch.setattr(settings, "registration_invite_token", "s3cret-invite-token")
    payload = _payload() if token is None else _payload(invite_token=token)
    resp = client.post(REGISTER, json=payload)
    assert resp.status_code == 403
    assert "invite token" in resp.json()["detail"].lower()


def test_register_accepted_with_the_right_invite_token(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_invite_token", "s3cret-invite-token")
    resp = client.post(REGISTER, json=_payload(invite_token="s3cret-invite-token"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "invitee@example.com"


def test_register_refused_in_production_without_an_invite_token(client, monkeypatch):
    """Fail closed: "enabled but tokenless" in production is a misconfiguration."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "registration_invite_token", "")
    resp = client.post(REGISTER, json=_payload())
    assert resp.status_code == 403
    assert "no invite token" in resp.json()["detail"].lower()
