"""Configuration hardening tests.

These verify that security-sensitive defaults are safe out of the box, so a
deploy that forgets to set an env var fails closed rather than open.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings


def test_debug_defaults_to_false():
    """Debug must be opt-in, never opt-out."""
    assert Settings.model_fields["debug"].default is False


def test_registration_defaults_to_closed():
    assert Settings.model_fields["registration_enabled"].default is False


def test_jwt_secret_rejected_in_production(monkeypatch):
    """The placeholder secret must not survive into production."""
    # Clear env so the default placeholder is used.
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production", _env_file=None)


def test_jwt_secret_accepted_in_development(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    s = Settings(environment="development", _env_file=None)
    assert s.jwt_secret == "change-me-to-a-long-random-string"


def test_positive_validators_reject_zero():
    with pytest.raises(ValidationError, match="positive"):
        Settings(
            environment="development",
            jwt_secret="test",
            access_token_expire_minutes=0,
            _env_file=None,
        )


def test_confidence_rejects_out_of_range():
    with pytest.raises(ValidationError, match="between 0 and 1"):
        Settings(
            environment="development",
            jwt_secret="test",
            autopilot_auto_publish_confidence=1.5,
            _env_file=None,
        )


def test_is_production_property():
    for env in ("production", "prod", "PRODUCTION", "Prod"):
        # Long enough to clear the production strength floor — this test is
        # about ``is_production``, not about the secret.
        s = Settings(
            environment=env,
            jwt_secret="real-secret-here" * 3,
            # Production refuses to start without one; this test is about
            # ``is_production``, not about credential encryption.
            token_encryption_key=Fernet.generate_key().decode(),
            _env_file=None,
        )
        assert s.is_production is True
    for env in ("development", "staging", "test"):
        s = Settings(environment=env, jwt_secret="test", _env_file=None)
        assert s.is_production is False


def test_database_pool_recycle_is_set_for_postgres():
    """Stale connections must be recycled so a PG restart doesn't break the pool."""
    from app.database import _make_engine

    pg_engine = _make_engine("postgresql+psycopg://test:test@localhost/test")
    try:
        assert pg_engine.pool._recycle >= 0, "pool_recycle should be set, not -1 (never)"
    finally:
        pg_engine.dispose()


def test_database_pool_skips_tuning_for_sqlite():
    from app.database import _make_engine

    sqlite_engine = _make_engine("sqlite://")
    # SQLite uses StaticPool by default which has no _recycle attr, or NullPool.
    # Just verify it doesn't crash.
    sqlite_engine.dispose()


def test_cors_origins_parsed():
    s = Settings(
        environment="development",
        jwt_secret="test",
        backend_cors_origins="http://a.com, http://b.com ,http://c.com",
        _env_file=None,
    )
    assert s.cors_origins == ["http://a.com", "http://b.com", "http://c.com"]
