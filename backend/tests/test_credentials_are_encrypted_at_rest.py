"""Platform credentials are encrypted at rest, and a box that cannot do it says so.

The write side was already fail-closed: :func:`encrypt_credentials` refuses to
store a credential in production without ``TOKEN_ENCRYPTION_KEY``. Two things it
could not do on its own:

* say so *early*. The refusal surfaced as a 500 the first time somebody
  connected a platform, which is both the wrong audience and the wrong moment;
* cover the read side. A blob with no ``fernet:v1:`` prefix is read back as
  plaintext JSON, so a box whose key went missing in a .env edit keeps serving
  every stored connection and nothing anywhere says the credentials are no
  longer encrypted.
"""
from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)

CREDS = {"token": "ghp_notarealtoken", "repo": "owner/name"}

#: Everything a production Settings needs before the key is what fails it.
_PRODUCTION = {
    "environment": "production",
    "jwt_secret": "x" * 48,
    "database_url": "postgresql+psycopg://u:p@localhost/pulse",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_PRODUCTION, **overrides})


# --------------------------------------------------------------------------- #
# Startup                                                                      #
# --------------------------------------------------------------------------- #


def test_production_refuses_to_start_without_an_encryption_key():
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY must be set"):
        _settings(token_encryption_key="")


def test_the_message_says_how_to_generate_one():
    """An operator reading this at 3am should not have to go and look it up."""
    with pytest.raises(ValidationError, match="Fernet.generate_key"):
        _settings(token_encryption_key="")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_whitespace_key_is_no_key(blank):
    """``crypto._cipher`` strips before using it, so this must strip before checking."""
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY must be set"):
        _settings(token_encryption_key=blank)


def test_a_malformed_key_is_refused_at_startup_not_at_first_use():
    """Wrong-length or non-base64 keys look fine until the moment they are used."""
    with pytest.raises(ValidationError, match="not a valid Fernet key"):
        _settings(token_encryption_key="not-a-fernet-key")


def test_a_good_key_starts():
    key = Fernet.generate_key().decode()
    assert _settings(token_encryption_key=key).token_encryption_key == key


@pytest.mark.parametrize("env", ["development", "staging", "test"])
def test_outside_production_no_key_is_needed(env):
    """The local-dev friction this setting is optional for is the whole point."""
    assert Settings(environment=env, token_encryption_key="").token_encryption_key == ""


@pytest.mark.parametrize("env", ["production", "prod", "PRODUCTION"])
def test_the_production_check_is_not_case_or_spelling_sensitive(env):
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY"):
        _settings(environment=env, token_encryption_key="")


# --------------------------------------------------------------------------- #
# Round trip                                                                   #
# --------------------------------------------------------------------------- #


def test_ciphertext_carries_the_prefix_and_not_the_secret(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings.token_encryption_key", Fernet.generate_key().decode()
    )
    stored = encrypt_credentials(CREDS)

    assert stored.startswith("fernet:v1:")
    assert "ghp_notarealtoken" not in stored
    assert decrypt_credentials(stored) == CREDS


def test_production_will_not_write_a_credential_without_a_key(monkeypatch):
    """The behaviour the startup check now makes unreachable. Pinned anyway:
    it is the last line of defence if a future config path skips validation."""
    monkeypatch.setattr("app.config.settings.token_encryption_key", "")
    monkeypatch.setattr("app.config.settings.environment", "production")

    with pytest.raises(CredentialEncryptionError, match="Refusing to store"):
        encrypt_credentials(CREDS)


# --------------------------------------------------------------------------- #
# Reading a row that was never encrypted                                       #
# --------------------------------------------------------------------------- #


def test_a_plaintext_row_read_in_production_is_logged_loudly(monkeypatch, caplog):
    """A restored dump or a lost key leaves live credentials in the clear.

    It still resolves — refusing would break publishing for a credential that
    works — but it may not do so silently, because silence is what makes
    "encrypted at rest" quietly stop being true.
    """
    monkeypatch.setattr("app.config.settings.environment", "production")

    with caplog.at_level(logging.WARNING):
        assert decrypt_credentials('{"token":"plain"}') == {"token": "plain"}

    assert any(
        "unencrypted row" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


def test_the_warning_does_not_name_the_credential(monkeypatch, caplog):
    """Logs are the one place a credential is most likely to be read by accident."""
    monkeypatch.setattr("app.config.settings.environment", "production")

    with caplog.at_level(logging.WARNING):
        decrypt_credentials('{"token":"ghp_notarealtoken"}')

    assert "ghp_notarealtoken" not in caplog.text


def test_development_reads_plaintext_without_the_warning(monkeypatch, caplog):
    """Keyless local runs store plaintext by design; warning on every read is noise."""
    monkeypatch.setattr("app.config.settings.environment", "development")

    with caplog.at_level(logging.WARNING):
        assert decrypt_credentials('{"token":"plain"}') == {"token": "plain"}

    assert "unencrypted row" not in caplog.text


def test_an_encrypted_row_never_takes_the_plaintext_path(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.config.settings.token_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr("app.config.settings.environment", "production")
    stored = encrypt_credentials(CREDS)

    with caplog.at_level(logging.WARNING):
        assert decrypt_credentials(stored) == CREDS

    assert "unencrypted row" not in caplog.text


def test_an_empty_column_is_not_a_plaintext_row(monkeypatch, caplog):
    """The model's default. A connection row can exist before credentials do."""
    monkeypatch.setattr("app.config.settings.environment", "production")

    with caplog.at_level(logging.WARNING):
        assert decrypt_credentials("") == {}

    assert "unencrypted row" not in caplog.text
