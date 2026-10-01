"""What the journal says about who tried to get in.

Every other credential decision in Pulse leaves a line. A replayed reset link,
an expired one and a completed reset are recorded by
:mod:`app.services.password_reset`; a rejected machine credential is recorded by
:mod:`app.services.api_keys`. ``POST /auth/login`` recorded nothing — which is
the one that matters most, because it is the endpoint an attacker actually
reaches for and the only one where the answer is deliberately uninformative to
the caller.

The practical shape of the hole: ten wrong passwords against a real account
produced ten 401s and *zero* log lines, and the first trace of the run anywhere
was slowapi's own ``ratelimit … exceeded`` line on the eleventh attempt. So the
signal existed only for the caller who was stopped, never for the nine who were
not — and a guesser pacing themselves under the limit was invisible for as long
as they cared to keep going.

Two properties are asserted here and both are load bearing:

* the line names the **rate-limit bucket**, the same string slowapi prints, so
  the refusals and the 429 that follows them grep as one run; and
* the line names the account by **id**, never by the address that was typed.
  The 401 refuses to say whether an address has an account here, and a journal
  full of attempted mailboxes would answer that question for anyone who ever
  reads a log file.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.user import User

LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"
AUTH_LOGGER = "app.routers.auth"


def _login(client, email: str, password: str):
    return client.post(LOGIN, data={"username": email, "password": password})


def _lines(caplog, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == AUTH_LOGGER and r.levelno == level
    ]


# --------------------------------------------------------------------------- #
# Refusals                                                                    #
# --------------------------------------------------------------------------- #


def test_a_wrong_password_is_a_warning_naming_the_account_by_id(client, user, caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        assert _login(client, user.email, "notthepassword").status_code == 401

    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert f"wrong password for user {user.id}" in warnings[0]


def test_an_unknown_address_says_so_without_repeating_the_address(client, caplog):
    """The reason distinguishes a spray from a targeted guess; the address is not in it."""
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        assert _login(client, "stranger@example.com", "whatever12345").status_code == 401

    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "no such account" in warnings[0]
    assert "stranger@example.com" not in warnings[0]


def test_a_wrong_password_never_puts_the_address_in_the_log_either(client, user, caplog):
    """The known-account arm must not leak the mailbox the id already identifies."""
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        _login(client, user.email, "notthepassword")

    assert user.email not in "\n".join(_lines(caplog, logging.WARNING))


def test_a_deactivated_account_trying_to_get_back_in_is_recorded(client, db, user, caplog):
    """A 403 here is the credentials being *right*, which is worth more than a 401."""
    user.is_active = False
    db.commit()

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        assert _login(client, user.email, "hunter2hunter2").status_code == 403

    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert f"user {user.id} is deactivated" in warnings[0]


def test_the_line_names_the_same_bucket_slowapi_will_name(client, user, caplog):
    """The refusals and the eventual 429 have to grep as one run.

    Both lines are captured in one block and compared to each other rather than
    to a literal address: the bucket is whatever :func:`app.ratelimit.client_key`
    resolves to for this transport, and the property being asserted is that
    Pulse's refusals and slowapi's limit line resolve it *the same way* — not
    what the answer happens to be. Eleven attempts, because the eleventh is the
    one slowapi refuses.
    """
    with caplog.at_level(logging.WARNING):
        for _ in range(11):
            _login(client, user.email, "notthepassword")

    refusals = _lines(caplog, logging.WARNING)
    limit_lines = [
        r.getMessage() for r in caplog.records if r.name == "slowapi"
    ]
    assert len(refusals) == 10
    assert limit_lines, "slowapi should have refused the eleventh attempt"

    # "login refused from <bucket>: …" against "ratelimit … (<bucket>) exceeded".
    bucket = refusals[0].removeprefix("login refused from ").split(":")[0]
    assert bucket
    assert f"({bucket})" in limit_lines[0]


def test_a_run_of_guesses_leaves_a_line_each_not_one_at_the_limit(client, user, caplog):
    """The whole point: the signal must not wait for the rate limiter.

    Nine attempts is deliberately under the ten-per-minute budget, so slowapi
    prints nothing and every line here is Pulse's own.
    """
    assert settings.rate_limit_login.startswith("10/minute")

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        for _ in range(9):
            assert _login(client, user.email, "notthepassword").status_code == 401

    assert len(_lines(caplog, logging.WARNING)) == 9


# --------------------------------------------------------------------------- #
# The successful half                                                          #
# --------------------------------------------------------------------------- #


def test_a_successful_sign_in_is_recorded_too(client, user, caplog):
    """Refusals alone cannot answer "did they get in?", which is the next question."""
    with caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        assert _login(client, user.email, "hunter2hunter2").status_code == 200

    infos = _lines(caplog, logging.INFO)
    assert len(infos) == 1
    assert f"login ok for user {user.id}" in infos[0]


def test_a_successful_sign_in_is_not_a_warning(client, user, caplog):
    """Otherwise a WARNING-floored journal reads every sign-in as a problem."""
    with caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        _login(client, user.email, "hunter2hunter2")

    assert _lines(caplog, logging.WARNING) == []


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def test_a_refused_registration_says_which_refusal_it_was(client, monkeypatch, caplog):
    """Closed, misconfigured and wrong-token are one 403 to the caller, not to the log.

    ``registration_enabled`` is pinned off rather than relied on: the repo's
    ``.env`` is read by ``Settings`` and reaches the suite, so "the default" is
    whatever the developer's own install is set to.
    """
    monkeypatch.setattr(settings, "registration_enabled", False)
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        resp = client.post(
            REGISTER, json={"email": "probe@example.com", "password": "longenough123"}
        )
    assert resp.status_code == 403

    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "registration refused" in warnings[0]
    assert "Registration is closed." in warnings[0]


def test_a_wrong_invite_token_is_distinguishable_in_the_log(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "registration_invite_token", "letmein")

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        resp = client.post(
            REGISTER,
            json={
                "email": "probe@example.com",
                "password": "longenough123",
                "invite_token": "wrong",
            },
        )
    assert resp.status_code == 403
    assert "Invalid invite token." in _lines(caplog, logging.WARNING)[0]


def test_an_account_that_is_created_says_so(client, db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "registration_invite_token", "letmein")

    with caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        resp = client.post(
            REGISTER,
            json={
                "email": "invitee@example.com",
                "password": "longenough123",
                "invite_token": "letmein",
            },
        )
    assert resp.status_code == 201, resp.text

    created = db.scalar(select(User).where(User.email == "invitee@example.com"))
    infos = _lines(caplog, logging.INFO)
    assert len(infos) == 1
    assert f"account {created.id} created" in infos[0]


def test_a_duplicate_signup_is_not_reported_as_an_account_being_created(
    client, user, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "registration_invite_token", "letmein")

    with caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        resp = client.post(
            REGISTER,
            json={
                "email": user.email,
                "password": "longenough123",
                "invite_token": "letmein",
            },
        )
    assert resp.status_code == 409
    assert _lines(caplog, logging.INFO) == []


@pytest.mark.parametrize("attempts", [1, 3])
def test_nothing_here_logs_the_password(client, user, caplog, attempts):
    """The one value that must never reach a log file, from either arm."""
    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        for _ in range(attempts):
            _login(client, user.email, "notthepassword")
        _login(client, user.email, "hunter2hunter2")

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "notthepassword" not in joined
    assert "hunter2hunter2" not in joined
