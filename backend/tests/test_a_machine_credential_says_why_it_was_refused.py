"""Why a machine call was turned away, written where an operator can read it.

The 401 a rejected API key gets back is deliberately one message for five
different facts — malformed, unknown prefix, wrong secret, revoked, expired,
account switched off. That is right: the reader might be holding a stolen token,
and telling them which of their guesses was once real is telling them too much.

It was also the *only* record of the decision, and that is the part that was
wrong. The operator reading the journal is not the person holding the token, and
"CI stopped publishing on Tuesday" had no answer anywhere in Pulse: an expired
key, a rotated one whose grace window has closed and a deactivated account are
the same silent 401, and none of them touches ``last_used_at`` — :func:`touch`
stamps only keys that were *accepted*, on purpose, so that an audit of the
column does not read as though a refused credential were working. The row that
knows why was the row nobody thought to look at.

So the reason goes to the log while staying out of the response, and the two
halves of that are tested separately here: the journal gains the reason
(:func:`test_an_expired_key_says_so_in_the_log` and friends) and the caller
gains nothing (:func:`test_the_response_still_says_nothing_it_did_not_before`).

What identifies the key in a log line is its **prefix**, which is stored in the
clear for exactly this purpose — it is the lookup handle, it is short enough to
read out over a call, and it is what a secret scanner greps for. The secret half
must never appear, which is what the last test in this file is for.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from app.models.api_key import ALL_SCOPES, ApiKeyScope
from app.models.mixins import utcnow
from app.services import api_keys

V1 = "/api/v1"
KEYS_LOGGER = "app.services.api_keys"
DEPS_LOGGER = "app.deps"


@pytest.fixture
def key(db, project):
    row, token = api_keys.mint(db, project=project, name="CI", scopes=list(ALL_SCOPES))
    db.commit()
    db.refresh(row)
    return row, token


def _headers(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


def _warnings(caplog, logger: str = KEYS_LOGGER) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == logger and r.levelno == logging.WARNING
    ]


# --------------------------------------------------------------------------- #
# The reasons an integration breaks on its own                                 #
# --------------------------------------------------------------------------- #


def test_an_expired_key_says_so_in_the_log(db, key, caplog):
    """The commonest way a working integration stops working by itself."""
    row, token = key
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    with caplog.at_level(logging.WARNING, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is None

    assert _warnings(caplog) == [f"api key {row.prefix} refused: expired"]


def test_a_revoked_key_is_distinguishable_from_an_expired_one(db, key, caplog):
    """Two different operator actions; the log must not collapse them.

    "Somebody revoked this" and "nobody did anything and the clock ran out" lead
    to different next steps, and the response cannot tell them apart by design.
    """
    row, token = key
    api_keys.revoke(db, row)

    with caplog.at_level(logging.WARNING, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is None

    assert _warnings(caplog) == [f"api key {row.prefix} refused: revoked"]


def test_a_key_expired_and_revoked_at_once_reports_the_revocation(db, key, caplog):
    """A deliberate act outranks the clock when both are true."""
    row, token = key
    row.expires_at = utcnow() - timedelta(days=2)
    api_keys.revoke(db, row)

    with caplog.at_level(logging.WARNING, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is None

    assert "revoked" in _warnings(caplog)[0]


def test_a_deactivated_account_taking_its_keys_with_it_is_recorded(db, user, key, caplog):
    """The key itself is untouched, so its row explains nothing on its own."""
    row, token = key
    user.is_active = False
    db.commit()

    with caplog.at_level(logging.WARNING, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is None

    assert _warnings(caplog) == [
        f"api key {row.prefix} refused: its account is deactivated"
    ]


def test_a_real_prefix_with_the_wrong_secret_is_a_warning(db, key, caplog):
    """Half a live credential is not routine: a truncated deploy secret, or worse."""
    row, token = key
    with caplog.at_level(logging.WARNING, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token[:-4] + "zzzz") is None

    assert _warnings(caplog) == [f"api key {row.prefix} refused: wrong secret"]


# --------------------------------------------------------------------------- #
# What must stay quiet                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "token",
    ["", "not-a-token", "hrld_short", "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x"],
)
def test_an_anonymous_probe_is_not_a_broken_integration(db, token, caplog):
    """A caller who presented nothing resembling a key must not raise a WARNING.

    Every machine route is reachable by anyone who can send a header, so this
    arm is the one an internet-facing install hits constantly. Logged at DEBUG,
    where it is available when somebody is actually debugging and invisible the
    rest of the time.
    """
    with caplog.at_level(logging.DEBUG, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is None

    assert _warnings(caplog) == []
    assert [r for r in caplog.records if r.name == KEYS_LOGGER]


def test_an_unknown_prefix_is_not_a_warning_either(db, caplog):
    """Shaped like ours but matching nothing: still a probe, not a failure."""
    _, invented = api_keys.generate()
    with caplog.at_level(logging.DEBUG, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, invented) is None

    assert _warnings(caplog) == []


def test_a_key_that_works_is_not_logged_as_a_refusal(db, key, caplog):
    row, token = key
    with caplog.at_level(logging.DEBUG, logger=KEYS_LOGGER):
        assert api_keys.authenticate(db, token) is row

    assert "refused" not in "\n".join(
        r.getMessage() for r in caplog.records if r.name == KEYS_LOGGER
    )


# --------------------------------------------------------------------------- #
# The scope refusal, which is the dependency's half                            #
# --------------------------------------------------------------------------- #


def test_a_scope_that_was_never_granted_is_recorded(client, db, project, caplog):
    """The 403 reaches a build step that prints nothing and exits non-zero.

    ``authenticate`` accepts this key, so without a line here the one refusal
    that is nobody's mistake but the minter's is the one missing from the
    account of why a machine call failed.
    """
    row, token = api_keys.mint(
        db, project=project, name="read only", scopes=[ApiKeyScope.CONTENT_READ]
    )
    db.commit()

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        resp = client.get(f"{V1}/machine/analytics", headers=_headers(token))

    assert resp.status_code == 403
    lines = _warnings(caplog, DEPS_LOGGER)
    assert len(lines) == 1
    assert row.prefix in lines[0]
    assert "analytics:read" in lines[0]


def test_a_key_with_the_scope_is_not_reported_as_missing_it(client, db, key, caplog):
    _, token = key
    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        assert client.get(f"{V1}/machine/whoami", headers=_headers(token)).status_code == 200

    assert _warnings(caplog, DEPS_LOGGER) == []


# --------------------------------------------------------------------------- #
# The two invariants that make all of the above safe                           #
# --------------------------------------------------------------------------- #


def test_the_secret_half_never_reaches_the_log(db, key, caplog):
    """The prefix is public; everything after it is not."""
    row, token = key
    secret = token.rsplit("_", 1)[1]
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    with caplog.at_level(logging.DEBUG):
        api_keys.authenticate(db, token)
        api_keys.authenticate(db, token[:-4] + "zzzz")
        api_keys.authenticate(db, "hrld_deadbeefcafe_" + secret)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in joined
    assert token not in joined


def test_the_response_still_says_nothing_it_did_not_before(client, db, key):
    """The log gained a reason; the caller must not have.

    An expired key, a revoked one and an invented one are one status and one
    message — otherwise the journal's candour has leaked into the API.
    """
    row, token = key
    _, invented = api_keys.generate()

    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    expired = client.get(f"{V1}/machine/whoami", headers=_headers(token))

    unknown = client.get(f"{V1}/machine/whoami", headers=_headers(invented))

    assert expired.status_code == unknown.status_code == 401
    assert expired.json() == unknown.json()
