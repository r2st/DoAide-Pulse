"""Encryption for platform credentials at rest.

Fernet (AES-128-CBC + HMAC) via the ``TOKEN_ENCRYPTION_KEY`` setting. The key is
optional in development because making people generate one before they can run
the app locally is friction that buys nothing — but a *production* process
without a key refuses to store a credential rather than writing an API token to
Postgres in the clear.

Ciphertext carries a version prefix so an unencrypted row written before a key
existed is still readable after one is added.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

#: Marks a payload as Fernet ciphertext. Anything without it is plaintext JSON
#: from a keyless development run.
_PREFIX = "fernet:v1:"


class CredentialEncryptionError(RuntimeError):
    """The credential could not be encrypted or decrypted."""


def _cipher() -> Fernet | None:
    key = settings.token_encryption_key.strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key — generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encryption_enabled() -> bool:
    """True when a usable key is configured."""
    return bool(settings.token_encryption_key.strip())


def encrypt_credentials(credentials: dict[str, Any]) -> str:
    """Serialize and encrypt a credential dict for storage."""
    raw = json.dumps(credentials, separators=(",", ":"), sort_keys=True)
    cipher = _cipher()
    if cipher is None:
        if settings.is_production:
            raise CredentialEncryptionError(
                "Refusing to store platform credentials unencrypted in production — "
                "set TOKEN_ENCRYPTION_KEY."
            )
        return raw
    return _PREFIX + cipher.encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_credentials(stored: str) -> dict[str, Any]:
    """Inverse of :func:`encrypt_credentials`. Returns ``{}`` for empty input."""
    if not stored:
        return {}
    if not stored.startswith(_PREFIX):
        # Written before a key was configured.
        if settings.is_production:
            # Production refuses to *write* one of these, so a production box
            # reading one means the row predates the key, arrived in a restored
            # dump, or was written while ENVIRONMENT said something else. It is
            # still usable and is deliberately still used — refusing here would
            # break publishing for a credential that works, and the remedy is
            # the same either way. But it is a live credential sitting in
            # Postgres in the clear, and the one thing it must not do is stay
            # quiet about it.
            logger.warning(
                "platform credentials read from an unencrypted row — re-save "
                "the connection in Settings to store it under "
                "TOKEN_ENCRYPTION_KEY"
            )
        try:
            return json.loads(stored)
        except ValueError as exc:
            raise CredentialEncryptionError("Stored credentials are unreadable") from exc

    cipher = _cipher()
    if cipher is None:
        raise CredentialEncryptionError(
            "Stored credentials are encrypted but TOKEN_ENCRYPTION_KEY is not set"
        )
    try:
        raw = cipher.decrypt(stored[len(_PREFIX) :].encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialEncryptionError(
            "Stored credentials could not be decrypted — TOKEN_ENCRYPTION_KEY has "
            "changed since they were saved. Reconnect the platform."
        ) from exc
    return json.loads(raw)
