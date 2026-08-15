"""Encryption for platform credentials at rest.

Fernet (AES-128-CBC + HMAC) via the ``TOKEN_ENCRYPTION_KEY`` setting. The key is
optional in development because making people generate one before they can run
the app locally is friction that buys nothing — but a *production* process
without a key refuses to store a credential rather than writing an API token to
Postgres in the clear.

Ciphertext carries a version prefix so an unencrypted row written before a key
existed is still readable after one is added.

**The setting holds a key list, newest first.** ``TOKEN_ENCRYPTION_KEY`` accepts
several keys separated by commas or whitespace; the first is what everything is
encrypted *under*, and every one of them is tried when reading. A single key —
which is what every existing deployment has — parses as a one-element list and
behaves exactly as before.

That list is the whole of what makes rotating the key survivable. With one key,
replacing it makes every stored platform credential, webhook signing secret and
inbound trigger secret unreadable at the same instant: publishing stops until
someone re-enters an API token they have to go and fetch again, and every
webhook receiver has to be updated with a new secret. With a list, the new key
goes on the front, the old one stays behind it, and nothing breaks while
``app.services.credential_rotation`` walks the stored rows and re-encrypts them
under the new key. Once that sweep reports nothing left, the old key comes out
of the list.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)

#: Marks a payload as Fernet ciphertext. Anything without it is plaintext JSON
#: from a keyless development run.
_PREFIX = "fernet:v1:"

#: What separates one key from the next in the setting. A Fernet key is
#: url-safe base64 — 44 characters of ``[A-Za-z0-9_=-]`` — so neither a comma
#: nor whitespace can appear inside one, and splitting on both means a value
#: pasted across two lines of a ``.env`` still parses.
_SEPARATORS = re.compile(r"[,\s]+")

_GENERATE_HINT = (
    'generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


class CredentialEncryptionError(RuntimeError):
    """The credential could not be encrypted or decrypted."""


def configured_keys() -> list[str]:
    """Every key in the setting, in order, newest first. May be empty."""
    return [k for k in _SEPARATORS.split(settings.token_encryption_key.strip()) if k]


def _fernets() -> list[Fernet]:
    """One :class:`Fernet` per configured key, or ``[]`` when none is set."""
    out: list[Fernet] = []
    for index, key in enumerate(configured_keys()):
        try:
            out.append(Fernet(key.encode("utf-8")))
        except (ValueError, TypeError) as exc:
            # Which one, by position — the value itself must not be logged or
            # raised, and with several keys "the key is bad" does not say enough
            # to fix it.
            where = "TOKEN_ENCRYPTION_KEY" if index == 0 else (
                f"key {index + 1} of TOKEN_ENCRYPTION_KEY"
            )
            raise CredentialEncryptionError(
                f"{where} is not a valid Fernet key — {_GENERATE_HINT}"
            ) from exc
    return out


def _cipher() -> MultiFernet | None:
    """Encrypts under the first key, decrypts with whichever one works."""
    keys = _fernets()
    if not keys:
        return None
    return MultiFernet(keys)


def _primary() -> Fernet | None:
    """The key new ciphertext is written under."""
    keys = _fernets()
    return keys[0] if keys else None


def encryption_enabled() -> bool:
    """True when a usable key is configured."""
    return bool(configured_keys())


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
            "Stored credentials could not be decrypted — no key in "
            "TOKEN_ENCRYPTION_KEY matches the one they were saved under. If the "
            "key was rotated, put the previous key back on the end of "
            "TOKEN_ENCRYPTION_KEY (comma-separated); otherwise reconnect the "
            "platform."
        ) from exc
    return json.loads(raw)


def is_current(stored: str) -> bool:
    """Whether *stored* is already encrypted under the *first* configured key.

    False for plaintext, for ciphertext written under a key that has since moved
    down the list, and for anything unreadable — all three are rows
    :func:`rewrap` has work to do on, and the caller wants one question answered,
    not three.

    True for an empty blob, which is a row that holds no secret rather than one
    holding a stale one — an inbound trigger with signature checking switched
    off is the ordinary case. Encrypting that would turn "no secret" into a
    perfectly good ciphertext of ``{}``.

    True when no key is configured at all: a keyless development box stores
    plaintext on purpose, and calling that out of date would have the rotation
    sweep rewrite every row into the same plaintext on every run.
    """
    if not stored:
        return True
    primary = _primary()
    if primary is None:
        return True
    if not stored.startswith(_PREFIX):
        return False
    try:
        primary.decrypt(stored[len(_PREFIX) :].encode("utf-8"))
    except InvalidToken:
        return False
    return True


def rewrap(stored: str) -> str:
    """Re-encrypt *stored* under the first configured key.

    Reads with the full key list and writes with the head of it, which is what
    moves a credential onto a new key without anyone re-entering it. Raises
    :class:`CredentialEncryptionError` if it cannot be read under any key — the
    caller decides whether that is worth stopping for, and the sweep does not.
    """
    return encrypt_credentials(decrypt_credentials(stored))
