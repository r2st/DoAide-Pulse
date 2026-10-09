"""Re-verification of stored platform connections.

Platform connections sit at CONNECTED until something tries them. On a quiet
project that can be weeks, and the first sign of a revoked token is a failed
publication at 3am. The ``verify_all_connections`` task catches it early.
"""
from __future__ import annotations

import pytest

from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import connection_verify
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import CredentialError, PublishError
from app.tasks import maintenance_tasks


def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    monkeypatch.setattr(maintenance_tasks, "SessionLocal", _no_close(db))


def _connection(db, user, *, platform=Platform.DEVTO):
    row = PlatformConnection(
        user_id=user.id,
        platform=platform,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "test-key"}),
        display_name="@test",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_valid_connection_stays_connected(db, user, monkeypatch):
    conn = _connection(db, user)
    assert conn.last_verified_at is None

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: type("A", (), {"verify": lambda self, c: "@test", "credential_fields": []})(),
    )
    result = connection_verify.verify_all(db)

    assert result == {"checked": 1, "valid": 1, "invalid": 0, "unreachable": 0}
    db.refresh(conn)
    assert conn.status == ConnectionStatus.CONNECTED
    assert conn.last_verified_at is not None
    assert conn.last_error is None


def test_a_revoked_credential_is_marked_invalid(db, user, monkeypatch):
    conn = _connection(db, user)

    def _reject(self, credentials):
        raise CredentialError("API key revoked")

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: type("A", (), {"verify": _reject, "credential_fields": []})(),
    )
    result = connection_verify.verify_all(db)

    assert result["invalid"] == 1
    db.refresh(conn)
    assert conn.status == ConnectionStatus.INVALID
    assert conn.last_verified_at is not None
    assert "revoked" in conn.last_error


def test_an_unreachable_platform_leaves_status_alone(db, user, monkeypatch):
    conn = _connection(db, user)

    def _down(self, credentials):
        raise PublishError("Connection timed out")

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: type("A", (), {"verify": _down, "credential_fields": []})(),
    )
    result = connection_verify.verify_all(db)

    assert result["unreachable"] == 1
    db.refresh(conn)
    assert conn.status == ConnectionStatus.CONNECTED


def test_multiple_connections_are_checked_independently(db, user, monkeypatch):
    """One dead token must not stop the others from being checked."""
    conn_devto = _connection(db, user, platform=Platform.DEVTO)
    conn_bluesky = _connection(db, user, platform=Platform.BLUESKY)

    call_count = {"devto": 0, "bluesky": 0}

    class FakeAdapter:
        credential_fields = []

        def __init__(self, platform):
            self._platform = platform

        def verify(self, credentials):
            if self._platform == Platform.DEVTO:
                call_count["devto"] += 1
                raise CredentialError("expired")
            call_count["bluesky"] += 1
            return "@ok"

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda p: FakeAdapter(p),
    )
    result = connection_verify.verify_all(db)

    assert result == {"checked": 2, "valid": 1, "invalid": 1, "unreachable": 0}
    assert call_count == {"devto": 1, "bluesky": 1}
    db.refresh(conn_devto)
    db.refresh(conn_bluesky)
    assert conn_devto.status == ConnectionStatus.INVALID
    assert conn_bluesky.status == ConnectionStatus.CONNECTED


def test_only_connected_connections_are_checked(db, user, monkeypatch):
    """An already-invalid connection is not re-checked."""
    conn = _connection(db, user)
    conn.status = ConnectionStatus.INVALID
    db.commit()

    verified = []

    class FakeAdapter:
        credential_fields = []

        def verify(self, credentials):
            verified.append(True)
            return "@test"

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: FakeAdapter(),
    )
    result = connection_verify.verify_all(db)

    assert result["checked"] == 0
    assert verified == []


def test_the_task_is_on_the_beat_schedule():
    from app.tasks.celery_app import celery_app

    scheduled = {
        entry["task"] for entry in celery_app.conf.beat_schedule.values()
    }
    assert "app.tasks.maintenance_tasks.verify_all_connections" in scheduled


def test_the_task_calls_the_service(db, user, monkeypatch):
    _connection(db, user)

    monkeypatch.setattr(
        "app.services.connection_verify.publishers.get_adapter",
        lambda _p: type("A", (), {"verify": lambda self, c: "@test", "credential_fields": []})(),
    )
    result = maintenance_tasks.verify_all_connections()

    assert result["checked"] == 1
    assert result["valid"] == 1
