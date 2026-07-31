"""Tests for security.py — password hashing and JWT token handling.

Covers the SHA-256 pre-hash, legacy fallback, expired tokens, tampered tokens,
and edge cases.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest

from app.config import settings
from app.security import (
    _prepare,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        pw = "correct-horse-battery"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("right")
        assert not verify_password("wrong", hashed)

    def test_long_password_handled(self):
        """Passwords longer than 72 bytes are correctly distinguished."""
        base = "a" * 80
        pw1 = base + "X"
        pw2 = base + "Y"
        h1 = hash_password(pw1)
        h2 = hash_password(pw2)
        assert verify_password(pw1, h1)
        assert verify_password(pw2, h2)
        assert not verify_password(pw1, h2)
        assert not verify_password(pw2, h1)

    def test_prepare_always_44_bytes(self):
        """SHA-256 → base64 should always be 44 bytes, under bcrypt limit."""
        for pw in ["short", "x" * 200, "", "unicode: café 日本語"]:
            prepared = _prepare(pw)
            assert len(prepared) == 44

    def test_corrupted_hash_returns_false(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")

    def test_empty_hash_returns_false(self):
        assert not verify_password("anything", "")


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token(42)
        subject = decode_access_token(token)
        assert subject == "42"

    def test_expired_token_returns_none(self):
        token = create_access_token(1, expires_minutes=-1)
        assert decode_access_token(token) is None

    def test_tampered_token_returns_none(self):
        token = create_access_token(1)
        # Flip a character in the payload section
        parts = token.split(".")
        payload = parts[1]
        tampered_char = "A" if payload[5] != "A" else "B"
        parts[1] = payload[:5] + tampered_char + payload[6:]
        tampered = ".".join(parts)
        assert decode_access_token(tampered) is None

    def test_wrong_secret_returns_none(self):
        token = create_access_token(1)
        with patch.object(settings, "jwt_secret", "different-secret-entirely"):
            assert decode_access_token(token) is None

    def test_wrong_token_type_returns_none(self):
        """A token with type != 'access' should be rejected."""
        payload = {
            "sub": "1",
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
            "type": "refresh",
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        assert decode_access_token(token) is None

    def test_missing_sub_returns_none(self):
        payload = {
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
            "type": "access",
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        assert decode_access_token(token) is None

    def test_token_includes_iat_claim(self):
        token = create_access_token(1)
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        assert "iat" in payload
