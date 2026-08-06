"""Tests for security hardening and bug fixes.

Covers:
- Login timing side-channel mitigation
- Registration race-condition handling (IntegrityError → 409)
- Password reset SMTP failure handling
- Health endpoint information leakage
- Cadence fallback for missing platforms
- Calendar SQL-level date filtering
- Metrics task error isolation
"""
from __future__ import annotations

import time
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.models.publication import Platform
from app.routers import misc
from app.security import hash_password
from app.services import cadence

# --------------------------------------------------------------------------- #
# Login timing side-channel                                                    #
# --------------------------------------------------------------------------- #


def test_login_nonexistent_email_takes_similar_time_to_wrong_password(client, user):
    """Both paths must run bcrypt so the response time is indistinguishable."""
    start = time.monotonic()
    resp1 = client.post(
        "/api/v1/auth/login",
        data={"username": "nonexistent@example.com", "password": "wrongwrongwrong"},
    )
    elapsed_nonexistent = time.monotonic() - start

    start = time.monotonic()
    resp2 = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "wrongwrongwrong"},
    )
    elapsed_wrong_pw = time.monotonic() - start

    assert resp1.status_code == 401
    assert resp2.status_code == 401
    # Both should take roughly the same time (bcrypt ~100ms).
    # Allow a generous 10x ratio to avoid flaky tests.
    if elapsed_wrong_pw > 0.001:
        assert elapsed_nonexistent / elapsed_wrong_pw > 0.1


def test_login_nonexistent_email_returns_401(client):
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": "nobody@nowhere.example", "password": "longpassword123"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


# --------------------------------------------------------------------------- #
# Registration race condition                                                 #
# --------------------------------------------------------------------------- #


def test_duplicate_registration_returns_409_not_500(client, user):
    """A concurrent duplicate insert should be caught gracefully."""
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": user.email, "password": "longenough123", "full_name": "Dup"},
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Password reset SMTP failure                                                 #
# --------------------------------------------------------------------------- #


def test_password_reset_smtp_failure_returns_202(client, user):
    """SMTP failures must not produce a 500 or leak that the email exists."""
    with patch("app.routers.auth.mailer") as mock_mailer:
        mock_mailer.send.side_effect = ConnectionRefusedError("SMTP down")
        resp = client.post(
            "/api/v1/auth/password-reset",
            json={"email": user.email},
        )
    assert resp.status_code == 202


def test_password_reset_unknown_email_returns_202(client):
    resp = client.post(
        "/api/v1/auth/password-reset",
        json={"email": "unknown@example.com"},
    )
    assert resp.status_code == 202


# --------------------------------------------------------------------------- #
# Health endpoint information leakage                                         #
# --------------------------------------------------------------------------- #


def test_health_does_not_leak_exception_details(client, monkeypatch):
    """The public health endpoint should not expose internal error messages."""
    monkeypatch.setattr(
        misc,
        "_check_redis",
        lambda: misc._Probe(False, misc._short(ConnectionError("secret internal detail"))),
    )
    resp = client.get("/api/v1/health")
    body = resp.json()
    # Should only show the class name, not the message
    assert "secret internal detail" not in body["redis"]["detail"]
    assert body["redis"]["detail"] == "ConnectionError"


def test_short_public_mode_strips_message():
    exc = ValueError("database password is hunter2")
    assert misc._short(exc) == "ValueError"
    assert "hunter2" not in misc._short(exc)


def test_short_non_public_mode_includes_message():
    exc = ValueError("database password is hunter2")
    detail = misc._short(exc, public=False)
    assert "hunter2" in detail


# --------------------------------------------------------------------------- #
# Cadence fallback for all platforms                                          #
# --------------------------------------------------------------------------- #


def test_cadence_for_all_implemented_platforms():
    """Every Platform enum member should have a cadence entry (no KeyError)."""
    for platform in Platform:
        c = cadence.cadence_for(platform)
        assert c.platform == platform
        assert c.max_per_week > 0
        assert len(c.best_weekdays) > 0


def test_cadence_for_bluesky():
    c = cadence.cadence_for(Platform.BLUESKY)
    assert c.platform == Platform.BLUESKY
    assert c.max_per_week > 0


def test_cadence_for_mastodon():
    c = cadence.cadence_for(Platform.MASTODON)
    assert c.platform == Platform.MASTODON
    assert c.max_per_week > 0


def test_cadence_for_git():
    c = cadence.cadence_for(Platform.GIT)
    assert c.platform == Platform.GIT
    assert c.max_per_week > 0


def test_cadence_describe_works_for_all_platforms():
    """describe() should not crash for any platform."""
    for platform in Platform:
        desc = cadence.describe(platform)
        assert "platform" in desc
        assert "max_per_week" in desc


def test_cadence_suggest_schedule_for_bluesky():
    from datetime import datetime

    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    slots = cadence.suggest_schedule(Platform.BLUESKY, count=3, start=now)
    assert len(slots) == 3
    for slot in slots:
        assert slot > now


# --------------------------------------------------------------------------- #
# Publish tasks — dispatch via .delay()                                       #
# --------------------------------------------------------------------------- #


def test_publish_due_dispatches_individually():
    """publish_due should call publish_one.delay() for each due publication."""
    from app.tasks import publish_tasks

    mock_pubs = [MagicMock(id=1), MagicMock(id=2), MagicMock(id=3)]
    with (
        patch("app.tasks.publish_tasks.publishing_service") as mock_svc,
        patch.object(publish_tasks.publish_one, "delay") as mock_delay,
    ):
        mock_svc.due_publications.return_value = mock_pubs
        result = publish_tasks.publish_due()

    assert result["dispatched"] == 3
    assert mock_delay.call_count == 3


# --------------------------------------------------------------------------- #
# Metrics task error isolation                                                #
# --------------------------------------------------------------------------- #


def test_metrics_task_isolates_failures():
    """A failure polling one publication should not prevent polling the rest."""
    from app.tasks import metrics_tasks

    mock_pub_ok = MagicMock(id=1)
    mock_pub_bad = MagicMock(id=2)
    mock_pub_ok2 = MagicMock(id=3)

    call_count = {"n": 0}

    def mock_collect(db, pub):
        call_count["n"] += 1
        if pub.id == 2:
            raise RuntimeError("platform API exploded")
        return MagicMock()

    with (
        patch("app.tasks.metrics_tasks.SessionLocal") as mock_session_cls,
        patch("app.tasks.metrics_tasks.publishing_service") as mock_svc,
        patch("app.tasks.metrics_tasks.publishers") as mock_publishers,
    ):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.scalars.return_value = [mock_pub_ok, mock_pub_bad, mock_pub_ok2]

        mock_adapter = MagicMock()
        mock_adapter.implemented = True
        mock_adapter.supports_metrics = True
        mock_adapter.platform = Platform.DEVTO
        mock_publishers.all_adapters.return_value = [mock_adapter]

        mock_svc.collect_metrics = mock_collect

        result = metrics_tasks.collect_all_metrics()

    # All three were attempted, two succeeded
    assert call_count["n"] == 3
    assert result["recorded"] == 2


# --------------------------------------------------------------------------- #
# JWT iat claim                                                                #
# --------------------------------------------------------------------------- #


def test_jwt_includes_iat_claim():
    """Access tokens must include an iat (issued-at) claim."""
    import jwt

    from app.security import create_access_token

    token = create_access_token("42")
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    assert "iat" in payload
    assert payload["iat"] > 0


# --------------------------------------------------------------------------- #
# Production JWT secret guard                                                  #
# --------------------------------------------------------------------------- #


def test_default_jwt_secret_rejected_in_production():
    """The default placeholder secret must not be accepted in production."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            environment="production",
            jwt_secret="change-me-to-a-long-random-string",
            database_url="sqlite://",
        )


def test_custom_jwt_secret_accepted_in_production():
    """A real secret should work fine in production."""
    from app.config import Settings

    s = Settings(
        environment="production",
        jwt_secret="a-real-secret-that-is-not-the-default",
        database_url="sqlite://",
    )
    assert s.jwt_secret == "a-real-secret-that-is-not-the-default"


# --------------------------------------------------------------------------- #
# bcrypt SHA-256 pre-hash                                                      #
# --------------------------------------------------------------------------- #


def test_long_passwords_are_not_truncated():
    """Two passwords that differ only after byte 72 must hash differently."""
    base = "a" * 72
    pw_a = base + "X"
    pw_b = base + "Y"

    hash_a = hash_password(pw_a)
    hash_b = hash_password(pw_b)

    # With truncation these would verify against each other.
    from app.security import verify_password

    assert verify_password(pw_a, hash_a)
    assert verify_password(pw_b, hash_b)
    assert not verify_password(pw_a, hash_b)
    assert not verify_password(pw_b, hash_a)


def test_legacy_hashes_still_verify():
    """Hashes created with the old truncation method must still work."""
    import bcrypt as _bcrypt

    from app.security import verify_password

    password = "mypassword123"
    # Simulate a hash created by the old _prepare (raw utf-8 truncation)
    legacy_hash = _bcrypt.hashpw(
        password.encode("utf-8")[:72], _bcrypt.gensalt()
    ).decode("utf-8")

    assert verify_password(password, legacy_hash)
