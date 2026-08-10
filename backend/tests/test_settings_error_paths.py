"""What the settings endpoints do when verification does not simply succeed.

:mod:`tests.test_settings` covers the happy path and the two validation
refusals. The arms below are the ones that only run when something outside
Herald misbehaves, and each maps to a distinct status code on purpose:

``501``
    The platform has no adapter yet. Nothing the user can do about it, and it is
    not their credentials that are wrong.
``502``
    The platform answered badly. The credentials might be perfect.
``500``
    Herald cannot encrypt what it was given — a misconfigured instance, not a
    bad token.

The distinction matters most on re-verification, where a wrong verdict is
destructive: flipping a connection to ``invalid`` because a platform had a bad
minute makes the user re-enter a token that was working.
"""
from __future__ import annotations

import pytest

from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import publishers
from app.services.crypto import CredentialEncryptionError
from app.services.publishers.base import (
    CredentialError,
    CredentialField,
    NotImplementedAdapter,
    PublishError,
)


@pytest.fixture
def adapter(monkeypatch):
    """The dev.to adapter, reduced to a single required field."""
    row = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        row,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )
    return row


def _raises(exc):
    def _verify(credentials):
        raise exc

    return _verify


def _connect(client, auth, key="fake-key"):
    return client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={"platform": "devto", "credentials": {"api_key": key}},
    )


# ---- PUT /connections ----------------------------------------------------- #


def test_an_unimplemented_platform_is_501_not_a_bad_credential(client, auth, adapter, monkeypatch):
    monkeypatch.setattr(
        adapter, "verify", _raises(NotImplementedAdapter("dev.to is not wired up yet"))
    )

    resp = _connect(client, auth)

    assert resp.status_code == 501
    assert "not wired up" in resp.json()["detail"]


def test_a_platform_having_a_bad_day_is_502_not_400(client, auth, adapter, monkeypatch):
    """The user's token may be fine — 400 would send them to rotate a good one."""
    monkeypatch.setattr(adapter, "verify", _raises(PublishError("502 from dev.to")))

    resp = _connect(client, auth)

    assert resp.status_code == 502
    assert "dev.to" in resp.json()["detail"]


def test_nothing_is_stored_when_the_platform_could_not_be_reached(
    client, auth, db, user, adapter, monkeypatch
):
    """A 502 must not leave a half-made connection row behind."""
    monkeypatch.setattr(adapter, "verify", _raises(PublishError("502 from dev.to")))

    _connect(client, auth)

    assert db.query(PlatformConnection).filter_by(user_id=user.id).count() == 0


def test_credentials_that_cannot_be_encrypted_are_500_and_not_stored(
    client, auth, db, user, adapter, monkeypatch
):
    """An instance with no encryption key in production must refuse, not store."""
    monkeypatch.setattr(adapter, "verify", lambda credentials: "test-user")
    monkeypatch.setattr(
        "app.routers.settings.encrypt_credentials",
        _raises(CredentialEncryptionError("set TOKEN_ENCRYPTION_KEY")),
    )

    resp = _connect(client, auth)

    assert resp.status_code == 500
    assert "TOKEN_ENCRYPTION_KEY" in resp.json()["detail"]
    assert db.query(PlatformConnection).filter_by(user_id=user.id).count() == 0


def test_reconnecting_updates_the_existing_row_rather_than_adding_one(
    client, auth, db, user, adapter, monkeypatch
):
    monkeypatch.setattr(adapter, "verify", lambda credentials: "first-name")
    assert _connect(client, auth, "one").status_code == 200

    monkeypatch.setattr(adapter, "verify", lambda credentials: "second-name")
    assert _connect(client, auth, "two").status_code == 200

    rows = db.query(PlatformConnection).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].display_name == "second-name"


# ---- POST /connections/{platform}/verify ---------------------------------- #


@pytest.fixture
def connected(db, user, adapter, monkeypatch, client, auth):
    """A stored, previously-good dev.to connection carrying an old error."""
    monkeypatch.setattr(adapter, "verify", lambda credentials: "test-user")
    assert _connect(client, auth).status_code == 200
    row = db.query(PlatformConnection).filter_by(user_id=user.id).one()
    row.last_error = "something that happened last week"
    db.commit()
    return row


def test_reverifying_clears_the_stale_error(client, auth, db, connected, adapter, monkeypatch):
    monkeypatch.setattr(adapter, "verify", lambda credentials: "renamed-user")

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected"
    assert body["display_name"] == "renamed-user"
    assert body["last_error"] is None


def test_a_rejected_credential_marks_the_connection_invalid(
    client, auth, db, connected, adapter, monkeypatch
):
    monkeypatch.setattr(adapter, "verify", _raises(CredentialError("401 Unauthorized")))

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert "401" in body["last_error"]


def test_an_undecryptable_blob_also_marks_it_invalid(
    client, auth, db, connected, adapter, monkeypatch
):
    """Same verdict as a rejected token: the stored credential is unusable."""
    # Imported inside the handler, so the module attribute is what it reads.
    monkeypatch.setattr(
        "app.services.crypto.decrypt_credentials",
        _raises(CredentialEncryptionError("key rotated out from under it")),
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert "rotated" in body["last_error"]


@pytest.mark.parametrize(
    "error",
    [
        PublishError("dev.to returned 503"),
        NotImplementedAdapter("no adapter for this platform"),
    ],
)
def test_an_unreachable_platform_records_the_error_without_invalidating(
    client, auth, db, connected, adapter, monkeypatch, error
):
    """The credentials were never judged, so the verdict must not change.

    This is the destructive-if-wrong arm: flipping to ``invalid`` here makes the
    user re-enter a token that works.
    """
    monkeypatch.setattr(adapter, "verify", _raises(error))

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected"
    assert body["last_error"]


def test_a_long_platform_error_is_clipped_before_it_is_stored(
    client, auth, db, connected, adapter, monkeypatch
):
    """``last_error`` is a column, not a log — an HTML error page must not fill it."""
    monkeypatch.setattr(adapter, "verify", _raises(PublishError("x" * 10_000)))

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200
    assert len(resp.json()["last_error"]) < 10_000


def test_verifying_another_users_connection_is_404(client, auth, db, adapter, monkeypatch):
    """Scoped by user, so somebody else's row is simply not there."""
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="somebody-else@example.com",
        hashed_password=hash_password("Str0ng-Passw0rd!"),
        full_name="Somebody Else",
    )
    db.add(other)
    db.flush()
    db.add(
        PlatformConnection(
            user_id=other.id,
            platform=Platform.DEVTO,
            encrypted_credentials="not-read-in-this-test",
            status=ConnectionStatus.CONNECTED,
            last_verified_at=utcnow(),
        )
    )
    db.commit()

    assert (
        client.post("/api/v1/settings/connections/devto/verify", headers=auth).status_code
        == 404
    )
    assert client.delete("/api/v1/settings/connections/devto", headers=auth).status_code == 404
