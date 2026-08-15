"""Two ways an auth endpoint answers a question it is refusing to answer.

Both endpoints here already say the right thing. ``/auth/login`` returns one
401 for a wrong password and an unknown address; ``/auth/password-reset``
returns one 202 whatever happened, with a body written not to confirm the
address. Neither wording is worth anything if the *timing* differs, and in both
cases it did:

* login verified an unknown address against a dummy hash pinned to cost 12,
  while the real hashes carry ``settings.bcrypt_rounds`` — the same number only
  by coincidence, and production is free to raise it;
* the reset endpoint made an SMTP round trip for an address that exists and
  returned immediately for one that does not.

The tests are structural rather than stopwatch-based on purpose. A wall-clock
assertion on bcrypt is a flake generator on shared CI, and it cannot express
the thing that actually matters — that the two paths do the *same work* — as
precisely as reading the work factor out of the hash or checking what is on the
request path at all.
"""
from __future__ import annotations

import pytest
from fastapi import BackgroundTasks

from app.config import settings
from app.security import _DUMMY_HASHES, dummy_hash, hash_password, verify_password
from app.services import mailer

LOGIN = "/api/v1/auth/login"
REQUEST_RESET = "/api/v1/auth/password-reset"


@pytest.fixture(autouse=True)
def _clear_dummy_cache():
    """The dummy hash is cached per work factor for the life of the process.

    Tests here change the factor, so the cache has to go with it or the second
    test reads the first one's answer.
    """
    _DUMMY_HASHES.clear()
    yield
    _DUMMY_HASHES.clear()


def _cost(hashed: str) -> int:
    """The work factor bcrypt recorded in the hash itself: ``$2b$12$...``."""
    return int(hashed.split("$")[2])


# --------------------------------------------------------------------------- #
# Login: the stand-in hash must cost what the real ones cost                   #
# --------------------------------------------------------------------------- #


def test_the_dummy_hash_carries_the_configured_work_factor():
    """The whole point of verifying against it is that it costs the same."""
    assert _cost(dummy_hash()) == settings.bcrypt_rounds
    assert _cost(hash_password("whatever")) == settings.bcrypt_rounds


@pytest.mark.parametrize("rounds", [4, 12, 14])
def test_the_dummy_hash_follows_the_setting_rather_than_a_literal(monkeypatch, rounds):
    """Production is held to *at least* 12, not exactly 12.

    A box raising BCRYPT_ROUNDS to 14 — a deliberate, sensible thing to do —
    used to make every real account four times slower to reject than an address
    with no account behind it, which is the enumeration signal the dummy verify
    exists to remove.
    """
    monkeypatch.setattr(settings, "bcrypt_rounds", rounds)
    _DUMMY_HASHES.clear()
    assert _cost(dummy_hash()) == rounds


def test_the_dummy_hash_is_cached_per_work_factor(monkeypatch):
    """Recomputed on every unknown address, it would double that path's cost.

    Which is the same bug wearing the other hat: an unknown address paying for
    two hashes where a known one pays for one is just as visible as paying for
    none.
    """
    first = dummy_hash()
    assert dummy_hash() is first

    monkeypatch.setattr(settings, "bcrypt_rounds", settings.bcrypt_rounds + 1)
    assert _cost(dummy_hash()) == settings.bcrypt_rounds


def test_no_password_verifies_against_the_dummy_hash():
    """It stands in for an account, so nothing may sign in as it."""
    for guess in ["", "password", "admin", dummy_hash()]:
        assert not verify_password(guess, dummy_hash())


def test_login_still_rejects_both_the_same_way(client, user):
    """The wording half of the defence, unchanged by the timing half."""
    unknown = client.post(
        LOGIN, data={"username": "nobody@nowhere.example", "password": "wrongwrong1"}
    )
    wrong = client.post(LOGIN, data={"username": user.email, "password": "wrongwrong1"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"] == "Incorrect email or password"


# --------------------------------------------------------------------------- #
# Password reset: SMTP must not be on the request path                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def deferred(monkeypatch) -> list[tuple]:
    """Capture background tasks instead of running them.

    Starlette's TestClient runs them before it hands the response back, so a
    test that only watched the outbox could not tell an inline send from a
    deferred one. Holding them unrun makes the distinction visible: anything
    that reaches the mailer anyway did so on the request path.
    """
    scheduled: list[tuple] = []

    def capture(self, func, *args, **kwargs) -> None:
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", capture)
    return scheduled


@pytest.fixture
def outbox(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(
        mailer,
        "send",
        lambda *, to, subject, body, html=None: sent.append({"to": to, "body": body})
        or True,
    )
    return sent


def test_a_reset_for_a_real_address_does_not_send_on_the_request_path(
    client, user, deferred, outbox
):
    """The SMTP round trip is the timing signal. It has to happen after the 202."""
    resp = client.post(REQUEST_RESET, json={"email": user.email})

    assert resp.status_code == 202
    assert outbox == [], "the mailer was called before the response was returned"
    assert len(deferred) == 1


def test_the_deferred_task_is_the_one_that_sends(client, user, deferred, outbox):
    """Deferring it must not lose it: run what was scheduled, get the email."""
    client.post(REQUEST_RESET, json={"email": user.email})

    func, args, kwargs = deferred[0]
    func(*args, **kwargs)

    assert len(outbox) == 1
    assert outbox[0]["to"] == user.email
    assert "reset-password?token=" in outbox[0]["body"]


def test_an_unknown_address_schedules_nothing_and_answers_the_same(
    client, user, deferred, outbox
):
    """The two requests now differ by a database lookup, not a network hop."""
    known = client.post(REQUEST_RESET, json={"email": user.email})
    unknown = client.post(REQUEST_RESET, json={"email": "nobody@nowhere.example"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(deferred) == 1
    assert outbox == []


def test_a_deactivated_account_schedules_nothing_and_answers_the_same(
    client, db, user, deferred, outbox
):
    """The third case the body covers, and the one most likely to be forgotten."""
    user.is_active = False
    db.commit()

    resp = client.post(REQUEST_RESET, json={"email": user.email})

    assert resp.status_code == 202
    assert deferred == []
    assert outbox == []


def test_a_dead_smtp_server_does_not_surface_to_the_caller(
    client, user, deferred, monkeypatch
):
    """Running after the response, a raising send has nothing to raise into.

    It still must not take the process's logger down with it, and — the part
    that matters here — must not have been able to turn a 202 into a 500 that
    says "this address exists".
    """
    def explode(*, to, subject, body, html=None):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "send", explode)

    resp = client.post(REQUEST_RESET, json={"email": user.email})
    assert resp.status_code == 202

    func, args, kwargs = deferred[0]
    func(*args, **kwargs)  # must not raise


def test_the_token_is_issued_before_the_response(client, db, user, deferred, outbox):
    """Issuing needs the request-scoped session, so it stays on the request path.

    Pinned because the obvious next tidy-up — move the whole block into the
    background task — would run it against a closed session and fail silently
    after a 202 has already promised an email.
    """
    from app.models.password_reset import PasswordResetToken

    client.post(REQUEST_RESET, json={"email": user.email})

    rows = db.query(PasswordResetToken).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].used_at is None
