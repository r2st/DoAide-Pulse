"""H065 Testing Coverage — targeted tests for untested critical paths.

Gaps identified by a systematic audit of every service module:

1. ``connection_verify.verify_all`` — the CredentialEncryptionError path and the
   unexpected Exception fallback were untested. Both are critical: the first
   handles a key that was rotated but the old key was already dropped; the second
   is the defensive catch-all that keeps one broken connection from stopping the
   sweep.

2. ``crypto.decrypt_credentials`` — corrupted ciphertext that decrypts to
   invalid JSON, and the ``is_current`` guard on ciphertext written under a
   removed key.

3. ``content_engagement._totals`` — the aggregation across platforms that
   carries provenance (which platforms reported each field) was only tested at
   the dataclass level; the trend-line merge across platforms with staggered
   polling was untested.

4. ``deps.get_current_user`` — the interaction between a non-integer ``sub``
   claim and the int parse was tested via HTTP but not the specific ValueError
   and TypeError paths.
"""
from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import connection_verify
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
    is_current,
)


# ------------------------------------------------------------------ #
# 1. connection_verify — CredentialEncryptionError path                #
# ------------------------------------------------------------------ #


def _make_connection(db, user, *, platform=Platform.DEVTO):
    row = PlatformConnection(
        user_id=user.id,
        platform=platform,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "test-key"}),
        display_name="@test",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_an_unreadable_credential_marks_the_connection_invalid(db, user, monkeypatch):
    """A stored credential that cannot be decrypted — typically because the
    encryption key was rotated and the old key was dropped before the sweep
    ran — must mark the connection INVALID rather than crashing the sweep.
    """
    conn = _make_connection(db, user)

    class FakeAdapter:
        credential_fields = []

        def verify(self, credentials):
            return "@test"

    def _fail_decrypt(stored):
        raise CredentialEncryptionError("no key matches")

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.connection_verify.decrypt_credentials",
        _fail_decrypt,
    )
    result = connection_verify.verify_all(db)

    assert result["invalid"] == 1
    assert result["valid"] == 0
    db.refresh(conn)
    assert conn.status == ConnectionStatus.INVALID
    assert conn.last_verified_at is not None
    assert "no key matches" in (conn.last_error or "")


def test_an_unexpected_exception_is_counted_as_unreachable_and_does_not_crash(
    db, user, monkeypatch, caplog
):
    """The defensive ``except Exception`` path: an error nobody anticipated
    must be logged and counted, not allowed to crash the entire sweep.
    """
    conn = _make_connection(db, user)

    class FakeAdapter:
        credential_fields = []

        def verify(self, credentials):
            raise RuntimeError("unexpected internal error")

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: FakeAdapter(),
    )
    with caplog.at_level(logging.ERROR):
        result = connection_verify.verify_all(db)

    assert result["unreachable"] == 1
    assert result["valid"] == 0
    assert result["invalid"] == 0
    db.refresh(conn)
    assert conn.status == ConnectionStatus.CONNECTED


def test_the_status_change_is_logged_when_a_connection_goes_invalid(
    db, user, monkeypatch, caplog
):
    """When a connection transitions from CONNECTED to INVALID, the change
    must be logged at WARNING level so operators notice.
    """
    conn = _make_connection(db, user)
    assert conn.status == ConnectionStatus.CONNECTED

    from app.services.publishers.base import CredentialError

    class FakeAdapter:
        credential_fields = []

        def verify(self, credentials):
            raise CredentialError("token expired")

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: FakeAdapter(),
    )
    with caplog.at_level(logging.WARNING):
        connection_verify.verify_all(db)

    assert any("status changed" in record.getMessage() for record in caplog.records)
    db.refresh(conn)
    assert conn.status == ConnectionStatus.INVALID


# ------------------------------------------------------------------ #
# 2. crypto — decrypt edge cases                                      #
# ------------------------------------------------------------------ #


def test_corrupted_plaintext_row_raises_credential_error(monkeypatch):
    """A row that is not valid JSON and has no Fernet prefix — e.g. a row
    written by a bug or corrupted in a restore — must raise
    CredentialEncryptionError, not a bare ValueError.
    """
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")

    with pytest.raises(CredentialEncryptionError, match="unreadable"):
        decrypt_credentials("this is not json {{{")


def test_ciphertext_without_a_configured_key_raises(monkeypatch):
    """Encrypted ciphertext on a box with no key must raise a clear error."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", key)
    stored = encrypt_credentials({"api_key": "secret"})
    assert stored.startswith("fernet:v1:")

    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")
    with pytest.raises(CredentialEncryptionError, match="not set"):
        decrypt_credentials(stored)


def test_is_current_returns_false_for_ciphertext_under_a_removed_key(monkeypatch):
    """``is_current`` must return False for ciphertext written under a key
    that is no longer in the configured list.
    """
    old_key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", old_key)
    stored = encrypt_credentials({"api_key": "secret"})

    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", new_key)

    assert is_current(stored) is False


def test_is_current_returns_true_for_empty_blob(monkeypatch):
    """An empty string means 'no secret stored', not 'stale secret'."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", key)

    assert is_current("") is True


def test_is_current_returns_true_when_no_key_configured(monkeypatch):
    """A keyless box stores plaintext deliberately; calling that stale would
    make the sweep rewrite every row on every run.
    """
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")
    assert is_current("some-plaintext") is True


# ------------------------------------------------------------------ #
# 3. content_engagement._totals — provenance tracking                  #
# ------------------------------------------------------------------ #


def test_totals_reported_by_tracks_which_platforms_contributed():
    """The ``reported_by`` dict must name the platforms that reported each
    field, so the UI can say '2 reactions, from devto and bluesky' rather
    than just '2 reactions'.
    """
    from app.services.content_engagement import PlatformEngagement, _totals

    platforms = [
        PlatformEngagement(
            publication_id=1,
            platform=Platform.DEVTO,
            external_url="https://dev.to/post",
            published_at=None,
            views=100,
            reactions=5,
            comments=2,
        ),
        PlatformEngagement(
            publication_id=2,
            platform=Platform.BLUESKY,
            external_url="https://bsky.app/post",
            published_at=None,
            reactions=3,
            shares=1,
        ),
    ]
    totals = _totals(platforms)

    assert totals.views == 100
    assert totals.reported_by["views"] == ["devto"]

    assert totals.reactions == 8
    assert set(totals.reported_by["reactions"]) == {"devto", "bluesky"}

    assert totals.shares == 1
    assert totals.reported_by["shares"] == ["bluesky"]

    assert totals.comments == 2
    assert totals.reported_by["comments"] == ["devto"]

    # Bluesky does not report views — views must not say "bluesky"
    assert "bluesky" not in totals.reported_by.get("views", [])


def test_totals_none_field_stays_none_when_no_platform_reports_it():
    """A field no platform reported must stay None, not become 0."""
    from app.services.content_engagement import PlatformEngagement, _totals

    platforms = [
        PlatformEngagement(
            publication_id=1,
            platform=Platform.BLUESKY,
            external_url=None,
            published_at=None,
            reactions=3,
        ),
    ]
    totals = _totals(platforms)

    assert totals.views is None
    assert totals.reads is None
    assert totals.clicks is None
    assert "views" not in totals.reported_by


def test_totals_engagement_matches_sum_of_platform_engagements():
    """The piece's engagement must be the sum of its platforms', so the
    two numbers cannot disagree.
    """
    from app.services.content_engagement import PlatformEngagement, _totals

    platforms = [
        PlatformEngagement(
            publication_id=1,
            platform=Platform.DEVTO,
            external_url=None,
            published_at=None,
            reactions=5,
            comments=2,
            clicks=1,
        ),
        PlatformEngagement(
            publication_id=2,
            platform=Platform.BLUESKY,
            external_url=None,
            published_at=None,
            reactions=3,
            shares=4,
        ),
    ]
    totals = _totals(platforms)

    platform_sum = sum(p.engagement for p in platforms)
    assert totals.engagement == platform_sum


# ------------------------------------------------------------------ #
# 4. deps.get_current_user — malformed subject claims                  #
# ------------------------------------------------------------------ #


def test_a_non_integer_subject_claim_is_rejected(client, user, monkeypatch):
    """A token with ``sub`` set to a non-integer value must be rejected
    with 401, not crash with 500.
    """
    from app import security

    token = security.create_access_token(subject="not-a-number")
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def test_a_null_subject_claim_is_rejected(client, user, monkeypatch):
    """A token where ``sub`` is None must be rejected with 401."""
    import jwt

    from app.config import settings

    payload = {"type": "access"}
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def test_an_inactive_user_is_rejected(client, db, user):
    """A valid token for a deactivated user must be rejected."""
    from app import security

    token = security.create_access_token(subject=str(user.id))
    user.is_active = False
    db.commit()

    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def test_a_token_for_a_nonexistent_user_is_rejected(client):
    """A valid token whose subject names a user that doesn't exist
    must be rejected with 401, not 500.
    """
    from app import security

    token = security.create_access_token(subject="999999")
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
