"""Tests for the credential encryption service.

Covers the full encrypt → decrypt round-trip, plaintext fallback in dev mode,
production guard, and error paths (wrong key, corrupted data, missing key for
encrypted data).
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
    encryption_enabled,
)


@pytest.fixture
def fernet_key(monkeypatch):
    """Provide a valid Fernet key via settings."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", key)
    return key


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", "")


def test_round_trip_with_key(fernet_key):
    creds = {"api_key": "secret-123", "org": "acme"}
    stored = encrypt_credentials(creds)
    assert stored.startswith("fernet:v1:")
    assert decrypt_credentials(stored) == creds


def test_plaintext_fallback_in_dev(no_key):
    # conftest sets ENVIRONMENT=development, so is_production is False
    creds = {"token": "abc"}
    stored = encrypt_credentials(creds)
    assert not stored.startswith("fernet:v1:")
    assert decrypt_credentials(stored) == creds


def test_refuses_plaintext_in_production(no_key, monkeypatch):
    from unittest.mock import PropertyMock, patch
    from app.config import Settings

    with patch.object(Settings, "is_production", new_callable=PropertyMock, return_value=True):
        with pytest.raises(CredentialEncryptionError, match="production"):
            encrypt_credentials({"token": "abc"})


def test_decrypt_with_wrong_key(fernet_key, monkeypatch):
    stored = encrypt_credentials({"x": "y"})
    # Swap to a different key
    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", new_key)
    with pytest.raises(CredentialEncryptionError, match="changed"):
        decrypt_credentials(stored)


def test_decrypt_empty_returns_empty_dict(fernet_key):
    assert decrypt_credentials("") == {}


def test_decrypt_encrypted_without_key(fernet_key):
    stored = encrypt_credentials({"k": "v"})
    # Remove the key
    from app.services import crypto
    from unittest.mock import patch
    with patch.object(crypto.settings, "token_encryption_key", ""):
        with pytest.raises(CredentialEncryptionError, match="not set"):
            decrypt_credentials(stored)


def test_decrypt_corrupted_plaintext(no_key):
    # conftest sets ENVIRONMENT=development, so is_production is already False
    with pytest.raises(CredentialEncryptionError, match="unreadable"):
        decrypt_credentials("not-valid-json{{{")


def test_encryption_enabled_flag(no_key):
    assert not encryption_enabled()


def test_encryption_enabled_with_key(fernet_key):
    assert encryption_enabled()


def test_invalid_fernet_key_raises(monkeypatch):
    monkeypatch.setattr(
        "app.services.crypto.settings.token_encryption_key", "not-a-valid-key"
    )
    with pytest.raises(CredentialEncryptionError, match="not a valid Fernet key"):
        encrypt_credentials({"x": "y"})
