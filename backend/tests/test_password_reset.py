"""The password reset flow.

No SMTP is reachable in tests, so :func:`app.services.mailer.send` is captured
and the token is read out of the message body — which also pins down that the
link the user receives is the one the API will accept.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.mixins import utcnow
from app.models.password_reset import PasswordResetToken
from app.security import verify_password
from app.services import mailer, password_reset

REQUEST = "/api/v1/auth/password-reset"
CONFIRM = "/api/v1/auth/password-reset/confirm"
LOGIN = "/api/v1/auth/login"
NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def outbox(monkeypatch) -> list[dict[str, str]]:
    """Capture what would have been emailed."""
    sent: list[dict[str, str]] = []

    def fake_send(*, to: str, subject: str, body: str) -> bool:
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(mailer, "send", fake_send)
    return sent


def _token_from(message: dict[str, str]) -> str:
    """The token as a user would get it: parsed out of the link in the email."""
    line = next(ln for ln in message["body"].splitlines() if "reset-password" in ln)
    return parse_qs(urlparse(line.strip()).query)["token"][0]


def _request_reset(client, email: str):
    return client.post(REQUEST, json={"email": email})


# --------------------------------------------------------------------------- #
# The happy path                                                              #
# --------------------------------------------------------------------------- #


def test_reset_link_sets_a_new_password(client, user, outbox):
    resp = _request_reset(client, user.email)
    assert resp.status_code == 202

    assert len(outbox) == 1
    assert outbox[0]["to"] == user.email
    token = _token_from(outbox[0])

    resp = client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text

    # The new password works and the old one does not.
    assert (
        client.post(
            LOGIN, data={"username": user.email, "password": NEW_PASSWORD}
        ).status_code
        == 200
    )
    assert (
        client.post(
            LOGIN, data={"username": user.email, "password": "hunter2hunter2"}
        ).status_code
        == 401
    )


def test_the_email_body_carries_a_usable_link(client, user, outbox):
    _request_reset(client, user.email)
    body = outbox[0]["body"]
    assert settings.frontend_url in body
    assert "/reset-password?token=" in body
    assert str(settings.password_reset_token_ttl_minutes) in body


# --------------------------------------------------------------------------- #
# Token handling                                                              #
# --------------------------------------------------------------------------- #


def test_only_a_hash_of_the_token_is_stored(client, db, user, outbox):
    _request_reset(client, user.email)
    token = _token_from(outbox[0])

    row = db.scalar(select(PasswordResetToken))
    assert row is not None
    assert token not in row.token_hash
    assert row.token_hash == password_reset.hash_token(token)
    assert len(row.token_hash) == 64


def test_a_token_works_only_once(client, user, outbox):
    _request_reset(client, user.email)
    token = _token_from(outbox[0])

    assert (
        client.post(
            CONFIRM, json={"token": token, "new_password": NEW_PASSWORD}
        ).status_code
        == 200
    )
    replay = client.post(
        CONFIRM, json={"token": token, "new_password": "another-password-entirely"}
    )
    assert replay.status_code == 400
    assert "already used" in replay.json()["detail"]


def test_requesting_again_invalidates_the_first_link(client, user, outbox):
    """Otherwise an old email keeps working after a second request."""
    _request_reset(client, user.email)
    first = _token_from(outbox[0])
    _request_reset(client, user.email)
    second = _token_from(outbox[1])
    assert first != second

    assert (
        client.post(
            CONFIRM, json={"token": first, "new_password": NEW_PASSWORD}
        ).status_code
        == 400
    )
    assert (
        client.post(
            CONFIRM, json={"token": second, "new_password": NEW_PASSWORD}
        ).status_code
        == 200
    )


def test_an_expired_token_is_refused(client, db, user, outbox):
    _request_reset(client, user.email)
    token = _token_from(outbox[0])

    row = db.scalar(select(PasswordResetToken))
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    resp = client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


def test_an_invented_token_is_refused(client, user):
    resp = client.post(
        CONFIRM, json={"token": "n0t-a-real-token-but-long-enough", "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 400


def test_a_short_password_is_rejected_before_the_token_is_spent(client, user, outbox):
    """A reset must not be a way around the registration password floor."""
    _request_reset(client, user.email)
    token = _token_from(outbox[0])

    assert (
        client.post(CONFIRM, json={"token": token, "new_password": "short"}).status_code
        == 422
    )
    # The token survived the rejected attempt.
    assert (
        client.post(
            CONFIRM, json={"token": token, "new_password": NEW_PASSWORD}
        ).status_code
        == 200
    )


def test_the_new_password_is_hashed_not_stored(client, db, user, outbox):
    _request_reset(client, user.email)
    token = _token_from(outbox[0])
    client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})

    db.refresh(user)
    assert user.hashed_password != NEW_PASSWORD
    assert verify_password(NEW_PASSWORD, user.hashed_password)


# --------------------------------------------------------------------------- #
# Not leaking who has an account                                              #
# --------------------------------------------------------------------------- #


def test_an_unknown_address_gets_the_same_answer(client, user, outbox):
    known = _request_reset(client, user.email)
    unknown = _request_reset(client, "nobody@example.com")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # ...and nothing was sent for the address that does not exist.
    assert [m["to"] for m in outbox] == [user.email]


def test_a_deactivated_account_gets_the_same_answer(client, db, user, outbox):
    user.is_active = False
    db.commit()

    resp = _request_reset(client, user.email)
    assert resp.status_code == 202
    assert outbox == []


def test_a_failed_send_does_not_change_the_answer(client, user, monkeypatch):
    """SMTP being down must not tell the requester anything either."""
    monkeypatch.setattr(mailer, "send", lambda **kwargs: False)
    resp = _request_reset(client, user.email)
    assert resp.status_code == 202
    assert "on its way" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Rate limiting and the mailer fallback                                       #
# --------------------------------------------------------------------------- #


def test_reset_requests_are_rate_limited(client, user, outbox):
    """5/hour — reset mail is a way to flood someone's inbox from outside."""
    for _ in range(5):
        assert _request_reset(client, user.email).status_code == 202
    assert _request_reset(client, user.email).status_code == 429


def test_mailer_logs_the_link_when_smtp_is_unconfigured(client, user, caplog):
    """The documented fallback for a self-hosted install with no mail server."""
    assert mailer.configured() is False  # no SMTP_HOST in tests

    with caplog.at_level("WARNING"):
        resp = _request_reset(client, user.email)
    assert resp.status_code == 202

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "SMTP is not configured" in logged
    assert "/reset-password?token=" in logged


def test_purge_expired_clears_spent_and_stale_rows(client, db, user, outbox):
    _request_reset(client, user.email)
    token = _token_from(outbox[0])
    client.post(CONFIRM, json={"token": token, "new_password": NEW_PASSWORD})

    assert db.scalar(select(PasswordResetToken)) is not None
    assert password_reset.purge_expired(db) == 1
    assert db.scalar(select(PasswordResetToken)) is None
