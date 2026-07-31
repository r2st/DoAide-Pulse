"""Maintenance task tests.

Exercises the housekeeping tasks that run on a daily beat schedule —
currently just expired-token cleanup.
"""
from __future__ import annotations

from datetime import timedelta

from app.models.mixins import utcnow
from app.models.password_reset import PasswordResetToken
from app.services.password_reset import hash_token, purge_expired


def test_purge_expired_removes_expired_tokens(db, user):
    """Tokens past their expiry should be purged."""
    expired = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token("expired-token"),
        expires_at=utcnow() - timedelta(hours=1),
    )
    db.add(expired)
    db.commit()

    count = purge_expired(db)
    assert count == 1
    assert db.get(PasswordResetToken, expired.id) is None


def test_purge_expired_removes_used_tokens(db, user):
    """Tokens that have been spent should also be cleaned up."""
    used = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token("used-token"),
        expires_at=utcnow() + timedelta(hours=1),
        used_at=utcnow() - timedelta(minutes=30),
    )
    db.add(used)
    db.commit()

    count = purge_expired(db)
    assert count == 1
    assert db.get(PasswordResetToken, used.id) is None


def test_purge_expired_keeps_live_tokens(db, user):
    """Active, unused tokens must survive the purge."""
    live = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token("live-token"),
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(live)
    db.commit()

    count = purge_expired(db)
    assert count == 0
    assert db.get(PasswordResetToken, live.id) is not None


def test_maintenance_module_imports():
    """Smoke test: the task module loads without import errors."""
    from app.tasks import maintenance_tasks  # noqa: F401
    assert hasattr(maintenance_tasks, "purge_expired_tokens")
