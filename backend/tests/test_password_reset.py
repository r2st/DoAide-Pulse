"""The password reset flow.

No SMTP is reachable in tests, so :func:`app.services.mailer.send` is captured
and the token is read out of the message body — which also pins down that the
link the user receives is the one the API will accept.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.mixins import as_aware, utcnow
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


# --------------------------------------------------------------------------- #
# Ending the sessions the reset was performed to end                          #
# --------------------------------------------------------------------------- #
#
# The threat this answers is the ordinary one: somebody resets their password
# *because* the account has been taken. Herald's access tokens are stateless
# JWTs with no revocation list, so before `tokens_valid_from` the reset changed
# what the next sign-in needed and nothing else — the attacker's bearer token
# went on working for the rest of ACCESS_TOKEN_EXPIRE_MINUTES.


def test_a_reset_stops_the_tokens_that_were_already_out(client, user, auth, outbox):
    """The whole point: a token that worked a moment ago must stop working."""
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 200

    _request_reset(client, user.email)
    resp = client.post(
        CONFIRM,
        json={"token": _token_from(outbox[0]), "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 200

    refused = client.get("/api/v1/auth/me", headers=auth)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Could not validate credentials"


def test_the_reset_does_not_lock_out_the_person_who_did_it(client, user, outbox):
    """The session opened *after* the reset has to work, or the fix is a lockout.

    The subtle half, and the reason `iat` is minted with microseconds rather
    than the whole seconds PyJWT writes by default: at one-second resolution
    this token and the one the previous test refuses carry the same `iat`, and
    no comparison can pass one without passing the other.
    """
    _request_reset(client, user.email)
    client.post(
        CONFIRM,
        json={"token": _token_from(outbox[0]), "new_password": NEW_PASSWORD},
    )

    fresh = client.post(LOGIN, data={"username": user.email, "password": NEW_PASSWORD})
    assert fresh.status_code == 200
    header = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=header).status_code == 200


def test_an_unrelated_account_keeps_its_session(client, db, user, auth, outbox):
    """One user's reset must not sign anybody else out."""
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="someone@example.com",
        full_name="Someone",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    other_login = client.post(
        LOGIN, data={"username": other.email, "password": "hunter2hunter2"}
    )
    other_auth = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    _request_reset(client, user.email)
    client.post(
        CONFIRM,
        json={"token": _token_from(outbox[0]), "new_password": NEW_PASSWORD},
    )

    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401
    assert client.get("/api/v1/auth/me", headers=other_auth).status_code == 200


def test_an_account_that_never_reset_accepts_its_tokens(client, user, auth, db):
    """NULL means "never changed", not "nothing is valid".

    The default for every row that predates the column, and getting it wrong
    signs the whole install out on deploy.
    """
    assert user.tokens_valid_from is None
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 200


def test_a_token_with_no_iat_is_refused_once_a_reset_has_happened(client, user, db):
    """A token Herald did not mint cannot be placed in time, so it cannot be
    shown to postdate the reset — and is refused rather than trusted."""
    import jwt

    from app.config import settings as app_settings

    user.tokens_valid_from = utcnow()
    db.commit()

    forged = jwt.encode(
        # Signature is good and it has not expired; only `iat` is missing.
        {"sub": str(user.id), "type": "access", "exp": utcnow() + timedelta(hours=1)},
        app_settings.jwt_secret,
        algorithm=app_settings.jwt_algorithm,
    )
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 401


@pytest.fixture(params=["Asia/Kolkata", "Pacific/Midway", "Pacific/Kiritimati"])
def local_timezone(request, monkeypatch):
    """Run the body on a box whose local clock is not UTC.

    Three offsets, two of them not whole hours away from each other and one on
    each side of the line, because the failure this guards is arithmetic: it
    shows up as the machine's own offset and vanishes on a UTC box.
    """
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def test_the_cutoff_is_read_as_utc_on_a_box_that_is_not(
    client, user, auth, outbox, local_timezone
):
    """The reset must revoke old sessions wherever the server happens to sit.

    ``tokens_valid_from`` is ``DateTime(timezone=True)``, but SQLite hands the
    offset back stripped, so what ``get_current_user`` loads is a *naive* UTC
    instant. ``datetime.timestamp()`` reads a naive value as **local** time. So
    a comparison written without :func:`app.models.mixins.as_aware` moves the
    cutoff by the machine's own offset — and east of Greenwich it moves it
    *backwards*, leaving every token minted in the last five and a half hours
    working after the reset that was supposed to kill them. That is the exact
    token this check exists to refuse, and on a UTC CI box every other test in
    this file passes while it is broken.

    The two assertions are the two halves that a wrong offset trades against
    each other: shift the cutoff one way and the attacker's token survives,
    shift it the other and the person who just reset their password is locked
    out of the session they opened afterwards.
    """
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 200

    _request_reset(client, user.email)
    assert client.post(
        CONFIRM,
        json={"token": _token_from(outbox[0]), "new_password": NEW_PASSWORD},
    ).status_code == 200

    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401

    fresh = client.post(LOGIN, data={"username": user.email, "password": NEW_PASSWORD})
    assert fresh.status_code == 200
    after = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=after).status_code == 200


def test_consume_stamps_the_cutoff(db, user):
    """The service records it, not just the endpoint — the trigger is the
    password changing, wherever that is driven from."""
    before = utcnow()
    raw = password_reset.issue(db, user)
    assert password_reset.consume(db, raw, NEW_PASSWORD) is not None
    db.refresh(user)
    assert user.tokens_valid_from is not None
    # ``as_aware``, not a bare ``.timestamp()``. The column is
    # ``DateTime(timezone=True)`` and SQLite hands the offset back stripped, so
    # the value that comes out of ``db.refresh`` here is a *naive* UTC instant.
    # ``datetime.timestamp()`` reads a naive value as **local** time, so this
    # assertion silently measured a different moment than the one that was
    # written — passing on a UTC box and failing by exactly the machine's offset
    # anywhere else. ``app.deps.get_current_user`` compares the same column the
    # same way for the same reason; this is the test saying it in the test's
    # voice rather than a second spelling of "what a cutoff is".
    assert as_aware(user.tokens_valid_from).timestamp() >= before.timestamp() - 1
