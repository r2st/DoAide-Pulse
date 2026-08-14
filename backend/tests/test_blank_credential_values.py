"""A blank credential value is not a way past the field checks.

``PUT /settings/connections`` validates the keys it is given against the
adapter's ``credential_fields``: unknown ones are refused so a typo is caught
while the user is still looking at the form, required ones must be present.

Both checks used to run against the *non-blank* keys only, which inverted the
unknown-field one: a misspelled key whose value happened to be blank was not
"supplied", so it was not unknown either — and the whole payload, misspelling
included, was encrypted and stored a few lines further down. The connection then
read as connected with the real field never set, which is the exact outcome the
check exists to prevent, reached by the exact payload a typo produces.

The second half is what "blank" means once a value is accepted. It means unset,
all the way down: a whitespace value stored against an optional field is worse
than a missing one, because ``credentials.get("branch") or "main"`` returns the
whitespace — `" "` is true.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.platform_connection import PlatformConnection
from app.models.publication import Platform
from app.services.crypto import decrypt_credentials
from app.services.publishers.base import CredentialField

CONNECTIONS = "/api/v1/settings/connections"


@pytest.fixture
def adapter(monkeypatch):
    """Dev.to with one required field and one optional one."""
    from app.services import publishers

    row = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(row, "verify", lambda creds: "test-user")
    monkeypatch.setattr(
        row,
        "credential_fields",
        [
            CredentialField(key="api_key", label="API Key", required=True),
            CredentialField(
                key="organization", label="Organization", required=False
            ),
        ],
    )
    return row


def _put(client, auth, credentials):
    return client.put(
        CONNECTIONS,
        headers=auth,
        json={"platform": "devto", "credentials": credentials},
    )


def _connection(db, user):
    return db.scalar(
        select(PlatformConnection).where(PlatformConnection.user_id == user.id)
    )


def _stored(db, user):
    return decrypt_credentials(_connection(db, user).encrypted_credentials)


# --------------------------------------------------------------------------- #
# The unknown-field check                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["", " ", "\t", "\n  \n"])
def test_an_unknown_field_is_refused_however_blank_its_value(
    client, auth, adapter, value
):
    resp = _put(client, auth, {"api_key": "ok", "api_kye": value})

    assert resp.status_code == 400
    assert "api_kye" in resp.json()["detail"]


def test_the_typo_is_not_stored_when_it_is_refused(client, auth, db, user, adapter):
    """The refusal has to happen before the encrypt, not alongside it."""
    assert _put(client, auth, {"api_key": "ok", "api_kye": " "}).status_code == 400

    assert _connection(db, user) is None


def test_a_blank_unknown_field_is_refused_even_when_it_is_the_only_key(
    client, auth, adapter
):
    resp = _put(client, auth, {"nonsense": "  "})

    assert resp.status_code == 400
    assert "nonsense" in resp.json()["detail"]


def test_an_unknown_field_with_a_real_value_is_still_refused(client, auth, adapter):
    """The case that always worked, kept working."""
    resp = _put(client, auth, {"api_key": "ok", "bogus": "nope"})

    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_every_unknown_field_is_named_not_just_the_first(client, auth, adapter):
    resp = _put(client, auth, {"api_key": "ok", "aaa": "", "zzz": " "})

    detail = resp.json()["detail"]
    assert "aaa" in detail and "zzz" in detail


# --------------------------------------------------------------------------- #
# The required-field check                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["", "   "])
def test_a_required_field_present_but_blank_still_counts_as_missing(
    client, auth, adapter, value
):
    resp = _put(client, auth, {"api_key": value})

    assert resp.status_code == 400
    assert "api_key" in resp.json()["detail"]


def test_the_unknown_check_runs_before_the_missing_one(client, auth, adapter):
    """A payload wrong in both ways is reported as the typo it is.

    Telling someone their ``api_key`` is missing when they clearly typed one,
    just under a misspelt name, sends them to look at the wrong field.
    """
    resp = _put(client, auth, {"api_kye": "ok"})

    detail = resp.json()["detail"]
    assert "api_kye" in detail
    assert "needs" not in detail


# --------------------------------------------------------------------------- #
# What is stored                                                               #
# --------------------------------------------------------------------------- #


def test_an_optional_field_left_blank_is_not_stored_as_whitespace(
    client, auth, db, user, adapter
):
    assert _put(client, auth, {"api_key": "ok", "organization": "   "}).status_code == 200

    assert _stored(db, user) == {"api_key": "ok"}


def test_a_stored_value_is_stripped(client, auth, db, user, adapter):
    """Otherwise a pasted token carries the newline that came with it."""
    assert _put(client, auth, {"api_key": "  ok\n"}).status_code == 200

    assert _stored(db, user) == {"api_key": "ok"}


def test_the_adapter_verifies_the_same_values_that_get_stored(
    client, auth, adapter, monkeypatch
):
    """Verifying one dict and encrypting another is how a connection is
    "verified" and then fails on its first real publish."""
    seen: dict[str, str] = {}

    def _verify(credentials):
        seen.update(credentials)
        return "test-user"

    monkeypatch.setattr(adapter, "verify", _verify)

    assert _put(client, auth, {"api_key": " ok ", "organization": " "}).status_code == 200

    assert seen == {"api_key": "ok"}


def test_an_optional_field_with_a_real_value_is_kept(client, auth, db, user, adapter):
    assert _put(client, auth, {"api_key": "ok", "organization": "acme"}).status_code == 200

    assert _stored(db, user) == {"api_key": "ok", "organization": "acme"}


def test_an_adapter_that_requires_nothing_still_refuses_an_all_blank_payload(
    client, auth, db, user, adapter, monkeypatch
):
    """`missing` is empty by definition here, so it cannot be what refuses this.

    Without its own check the payload reduces to ``{}``, which encrypts fine and
    presents as a connected platform holding no credentials at all.
    """
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=False)],
    )

    resp = _put(client, auth, {"api_key": "   "})

    assert resp.status_code == 400
    assert "at least one credential" in resp.json()["detail"]
    assert _connection(db, user) is None


def test_a_completely_empty_credentials_object_is_refused_by_the_schema(
    client, auth, adapter
):
    """``ConnectionCreate.credentials`` has ``min_length=1``, so this is a 422."""
    assert _put(client, auth, {}).status_code == 422
