"""An address stored in one case must be usable in another.

The bug this file pins was an authentication lockout with no way out of it. An
account created as ``Casey@Example.com`` — from the seed, from a registration
form, from anywhere someone typed their own address the way they write it —
could not sign in as ``casey@example.com``. Both lookups were
``User.email == <what they typed>`` against a case-sensitive column, so the case
the row happened to be written in had quietly become part of the credential.

What makes it a lockout rather than a papercut is the pair of answers. Login
says "Incorrect email or password", which is a true statement about a password
that was never wrong and sends the owner to reset it. The reset then says "if
that address has an account, a reset link is on its way" — deliberately vague,
correctly so, and in this case describing a lookup that matched nothing and
issued no token. Neither answer can be distinguished from the ordinary failure
it imitates, so there is nowhere to look next.

See :mod:`app.services.accounts` for why the fix reads exact-first and writes
normalized, and why no migration rewrites the rows that are already there.
"""
from __future__ import annotations

import pytest

from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.security import hash_password
from app.services import accounts

V1 = "/api/v1"

PASSWORD = "hunter2hunter2"


@pytest.fixture
def mixed_case(db) -> User:
    """An account whose stored address carries capitals.

    Written straight to the database rather than through registration, because
    registration now normalizes and this is the row shape that predates it —
    exactly what a live install can be holding.
    """
    row = User(
        email="Casey@Example.com",
        full_name="Casey",
        hashed_password=hash_password(PASSWORD),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# The lockout                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "typed",
    [
        "casey@example.com",
        "CASEY@EXAMPLE.COM",
        "Casey@Example.com",
        "cAsEy@eXaMpLe.CoM",
        # Trailing whitespace: what an autofill or a copy-paste out of a mail
        # client hands the form.
        "  casey@example.com  ",
    ],
    ids=["lower", "upper", "exact", "mixed", "padded"],
)
def test_the_owner_signs_in_however_they_type_their_address(client, mixed_case, typed):
    resp = client.post(f"{V1}/auth/login", data={"username": typed, "password": PASSWORD})

    assert resp.status_code == 200, (
        f"signing in as {typed!r} was refused for an account stored as "
        f"{mixed_case.email!r} — the case an address was written in is not a "
        f"credential"
    )
    assert resp.json()["access_token"]


def test_the_password_is_still_the_password(client, mixed_case):
    """The guard on the test above: matching the address more loosely must not
    make anything else looser."""
    resp = client.post(
        f"{V1}/auth/login",
        data={"username": "casey@example.com", "password": "wrongwrongwrong"},
    )
    assert resp.status_code == 401


def test_a_reset_reaches_the_account_whatever_case_is_typed(client, db, mixed_case):
    """The way out of the lockout, which was itself shut.

    A 202 here proves nothing on its own — it is what this endpoint answers for
    an address with no account at all. The assertion is that a token was
    actually minted for the right user.
    """
    resp = client.post(f"{V1}/auth/password-reset", json={"email": "casey@example.com"})
    assert resp.status_code == 202

    issued = db.query(PasswordResetToken).all()
    assert len(issued) == 1, (
        "the reset request matched no account, and said so in the same words it "
        "uses when the address is genuinely unknown"
    )
    assert issued[0].user_id == mixed_case.id


def test_a_deactivated_account_is_still_deactivated(client, db, mixed_case):
    """Reached case-insensitively now, and refused for the reason it always was."""
    mixed_case.is_active = False
    db.commit()

    resp = client.post(
        f"{V1}/auth/login", data={"username": "casey@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Writes: one mailbox is one account                                           #
# --------------------------------------------------------------------------- #


def test_registration_stores_the_address_normalized(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.registration_enabled", True)
    monkeypatch.setattr("app.config.settings.registration_invite_token", "")
    monkeypatch.setattr("app.config.settings.environment", "development")

    resp = client.post(
        f"{V1}/auth/register",
        json={"email": "  New.Person@Example.COM ", "password": "longenough123"},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "new.person@example.com"


def test_the_same_mailbox_in_another_case_is_a_duplicate(client, monkeypatch, mixed_case):
    """Not a second account.

    The unique index cannot see this: ``casey@example.com`` and
    ``Casey@Example.com`` are two different strings to Postgres. Left to it,
    registration would build a second account on one mailbox — and then a reset
    for that address becomes ambiguous between them.
    """
    monkeypatch.setattr("app.config.settings.registration_enabled", True)
    monkeypatch.setattr("app.config.settings.registration_invite_token", "")
    monkeypatch.setattr("app.config.settings.environment", "development")

    resp = client.post(
        f"{V1}/auth/register",
        json={"email": "casey@example.com", "password": "longenough123"},
    )

    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# The lookup's own rules                                                       #
# --------------------------------------------------------------------------- #


def test_an_exact_match_wins_over_a_case_insensitive_one(db, mixed_case):
    """The ordering that makes this safe on a database written before it.

    Nothing stopped an install from ending up with both spellings as separate
    accounts. Each one must still resolve to itself — a case-insensitive match
    that ran first would hand one person the other's row.
    """
    other = User(
        email="casey@example.com",
        full_name="Someone else entirely",
        hashed_password=hash_password(PASSWORD),
    )
    db.add(other)
    db.commit()

    assert accounts.find_by_email(db, "Casey@Example.com").id == mixed_case.id
    assert accounts.find_by_email(db, "casey@example.com").id == other.id


def test_an_ambiguous_address_resolves_to_nobody(db, mixed_case):
    """Two rows, no exact match, so there is no answer — and ``None`` is it.

    Returning either row would be picking whichever the planner listed first and
    calling it an identity. The address is unresolvable; a lockout is the right
    outcome and a coin toss between two accounts is not.
    """
    db.add(
        User(
            email="CASEY@EXAMPLE.COM",
            full_name="Someone else entirely",
            hashed_password=hash_password(PASSWORD),
        )
    )
    db.commit()

    assert accounts.find_by_email(db, "casey@example.com") is None


def test_an_ambiguous_address_is_still_taken(db, mixed_case):
    """``email_taken`` asks a different question than ``find_by_email``.

    The address above resolves to no account, and registration must still refuse
    it — otherwise the answer to an ambiguous mailbox is a third row on it.
    """
    db.add(
        User(
            email="CASEY@EXAMPLE.COM",
            full_name="Someone else entirely",
            hashed_password=hash_password(PASSWORD),
        )
    )
    db.commit()

    assert accounts.find_by_email(db, "casey@example.com") is None
    assert accounts.email_taken(db, "casey@example.com") is True


def test_an_unknown_address_is_nobody(db, mixed_case):
    assert accounts.find_by_email(db, "nobody@example.com") is None
    assert accounts.email_taken(db, "nobody@example.com") is False
