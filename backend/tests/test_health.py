"""The /health endpoint's dependency probes.

Caddy uses this as its `health_uri`, so the status code decides whether the
upstream stays in the pool. Both the body and the code are asserted.

The probes are monkeypatched rather than run for real: whether a Redis happens
to be listening on the developer's machine is not something these tests should
depend on.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.routers import misc

HEALTH = "/api/v1/health"


@pytest.fixture
def redis_up(monkeypatch):
    monkeypatch.setattr(misc, "_check_redis", lambda: misc._Probe(True))


@pytest.fixture
def redis_down(monkeypatch):
    monkeypatch.setattr(
        misc, "_check_redis", lambda: misc._Probe(False, "ConnectionError: refused")
    )


def test_healthy_when_both_dependencies_answer(client, redis_up):
    resp = client.get(HEALTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "ok"
    assert body["redis"]["status"] == "ok"


def test_database_down_fails_the_check(client, redis_up, monkeypatch):
    """Postgres is required unconditionally: no database, no Herald."""
    monkeypatch.setattr(misc, "_check_database", lambda db: misc._Probe(False, "boom"))
    resp = client.get(HEALTH)
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == {"status": "unavailable", "required": True, "detail": "boom"}


def test_real_database_probe_reports_a_broken_session(db, monkeypatch):
    """The probe itself, against a session whose execute fails.

    The session factory is lazy — it connects on first execute — so a probe that
    only opened a Session would report ok against a stopped Postgres. This is
    what makes ``SELECT 1`` load-bearing.
    """
    def explode(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(db, "execute", explode)
    probe = misc._check_database(db)
    assert probe.ok is False
    assert "OperationalError" in probe.detail


def test_redis_down_fails_the_check_when_celery_is_enabled(
    client, redis_down, monkeypatch
):
    """With workers running, a dead broker means publishes queue into nothing."""
    monkeypatch.setattr(settings, "celery_enabled", True)
    resp = client.get(HEALTH)
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["redis"]["required"] is True
    assert "ConnectionError" in body["redis"]["detail"]


def test_redis_down_is_tolerated_when_celery_is_disabled(client, redis_down):
    """Single-process deployments run everything inline; Redis is optional."""
    assert settings.celery_enabled is False  # conftest
    resp = client.get(HEALTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["redis"] == {
        "status": "unavailable",
        "required": False,
        "detail": "ConnectionError: refused",
    }


def test_redis_probe_swallows_a_bad_url(monkeypatch):
    """Any failure is a failed probe, not a 500 on the health endpoint."""
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 0.05)
    probe = misc._check_redis()
    assert probe.ok is False
    assert probe.detail


def test_detail_is_one_truncated_line():
    exc = ValueError("line one\nline two with a secret-looking string")
    detail = misc._short(exc)
    assert detail == "ValueError: line one"
    assert len(misc._short(ValueError("x" * 500))) <= 200


def test_health_still_reports_capabilities(client, redis_up):
    body = client.get(HEALTH).json()
    # conftest blanks every key, so the chain is empty in tests.
    assert body["llm_providers"] == []
    assert set(body["implemented_platforms"]) == {"devto", "medium"}
    assert body["github_configured"] is False
