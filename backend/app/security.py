"""Authentication primitives: password hashing and JWT access tokens."""
from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.config import settings

# bcrypt silently ignores bytes past position 72. Two passwords that differ
# only after byte 72 would verify against the same hash — a subtle but real
# bug. Pre-hashing with SHA-256 compresses any-length input to 44 base64
# bytes, well within bcrypt's limit, and is the same technique used by
# Dropbox and Django.
_BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    """SHA-256 pre-hash so passwords >72 bytes are handled correctly.

    The output is always 44 base64 bytes — well within bcrypt's 72-byte limit.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def _prepare_legacy(password: str) -> bytes:
    """Original preparation: raw UTF-8 truncated at 72 bytes.

    Kept for backward compatibility with hashes created before the SHA-256
    pre-hash was introduced. New hashes always use :func:`_prepare`.
    """
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash for a plaintext password."""
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    Tries the current SHA-256 pre-hash first, then the legacy truncation
    method for hashes created before the migration. This lets existing
    users log in without a forced password reset.
    """
    hashed_bytes = hashed.encode("utf-8")
    try:
        if bcrypt.checkpw(_prepare(plain), hashed_bytes):
            return True
        # Fall back to the legacy method for old hashes.
        return bcrypt.checkpw(_prepare_legacy(plain), hashed_bytes)
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str | int, expires_minutes: int | None = None) -> str:
    """Create a signed JWT whose ``sub`` claim is the user id."""
    now = datetime.now(UTC)
    expire = now + timedelta(
        minutes=expires_minutes or settings.access_token_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "iat": now,
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str | None:
    """Return the subject (user id) of a valid token, or ``None`` if invalid."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != "access":
        return None
    return payload.get("sub")
