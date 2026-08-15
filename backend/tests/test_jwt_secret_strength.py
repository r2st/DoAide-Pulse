"""Production must refuse a JWT secret that is short, not merely non-default.

``JWT_SECRET`` signs every session token Herald issues, with HMAC-SHA256. The
production guard only ever compared against the placeholder string, so
``JWT_SECRET=hunter2`` started cleanly — and a key shorter than the hash output
can be recovered offline from a single issued token, which is the whole of the
auth scheme. PyJWT notices (``InsecureKeyLengthWarning``) and signs anyway, so
nothing downstream was going to catch it.

RFC 7518 §3.2 sets the floor at the hash output size: 256 bits, 32 bytes.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import _MIN_JWT_SECRET_BYTES, Settings

#: Comfortably over the floor, and not the placeholder.
_STRONG = "x" * _MIN_JWT_SECRET_BYTES


def _settings(**kwargs):
    """A Settings built for the JWT rules and nothing else.

    ``token_encryption_key`` is supplied because production refuses to start
    without one, and every production case below is about the secret. An
    explicit value in *kwargs* still wins.
    """
    return Settings(
        database_url="sqlite://",
        token_encryption_key=Fernet.generate_key().decode(),
        _env_file=None,
        **kwargs,
    )


@pytest.mark.parametrize(
    "secret",
    [
        "hunter2",
        "short",
        "x" * (_MIN_JWT_SECRET_BYTES - 1),
    ],
)
def test_a_short_secret_is_refused_in_production(secret):
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(environment="production", jwt_secret=secret)


def test_the_refusal_says_how_to_generate_a_good_one():
    """An error that stops a deploy has to say what to do next."""
    with pytest.raises(ValidationError) as exc:
        _settings(environment="production", jwt_secret="hunter2")
    message = str(exc.value)
    assert str(_MIN_JWT_SECRET_BYTES) in message
    assert "token_urlsafe" in message


def test_a_secret_exactly_at_the_floor_is_accepted():
    """The bound is inclusive — 32 bytes is the requirement, not just under it."""
    s = _settings(environment="production", jwt_secret=_STRONG)
    assert s.jwt_secret == _STRONG


def test_the_default_placeholder_is_still_refused_by_name():
    """It is long enough to clear the floor, so length must not be the only check.

    The placeholder is 33 characters. Had the length test replaced the identity
    test rather than joining it, the one secret guaranteed to be public would
    have started passing.
    """
    placeholder = Settings.model_fields["jwt_secret"].default
    assert len(placeholder.encode("utf-8")) >= _MIN_JWT_SECRET_BYTES, (
        "this test is only meaningful while the placeholder clears the floor"
    )
    with pytest.raises(ValidationError, match="default value"):
        _settings(environment="production", jwt_secret=placeholder)


@pytest.mark.parametrize("env", ["development", "staging", "test"])
def test_a_short_secret_is_allowed_outside_production(env):
    """Local runs and the test suite must not need a generated key.

    The guard is about what ships, and requiring one to run the app locally is
    friction that buys nothing — the same reasoning ``TOKEN_ENCRYPTION_KEY``
    already follows.
    """
    s = _settings(environment=env, jwt_secret="short")
    assert s.jwt_secret == "short"


def test_a_multibyte_secret_is_measured_in_bytes_not_characters():
    """Length is checked on the encoded key, because that is what HMAC consumes.

    31 astral-plane characters are 124 bytes of key material and plenty; 31
    ASCII ones are 31 bytes and not. Counting characters would have got both
    wrong in the direction that matters for one of them.
    """
    wide = "🔑" * ((_MIN_JWT_SECRET_BYTES // 4) + 1)
    assert len(wide) < _MIN_JWT_SECRET_BYTES  # fewer characters than the floor
    s = _settings(environment="production", jwt_secret=wide)
    assert s.jwt_secret == wide


def test_the_suites_own_secret_clears_the_production_floor():
    """The fixture key is short-secret-legal, but it must not be short.

    Nothing forces this — ``ENVIRONMENT=development`` in ``conftest`` means the
    validator above waves any length through. It is pinned anyway for a reason
    that is not about strength: PyJWT emits ``InsecureKeyLengthWarning`` on
    every ``decode()`` under a sub-32-byte key, and almost every test in this
    suite authenticates. The 26-byte value this fixture used to carry produced
    roughly seven hundred warnings a run — enough that a genuine one arriving
    from anywhere else would have scrolled past unread.
    """
    from app.config import settings

    assert len(settings.jwt_secret.encode("utf-8")) >= _MIN_JWT_SECRET_BYTES, (
        "tests/conftest.py must set a JWT_SECRET of at least "
        f"{_MIN_JWT_SECRET_BYTES} bytes, or the suite drowns in PyJWT warnings"
    )


def test_prod_is_spelled_both_ways():
    """``ENVIRONMENT=prod`` must not be a way around the check."""
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(environment="prod", jwt_secret="hunter2")
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(environment="PRODUCTION", jwt_secret="hunter2")
