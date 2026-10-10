"""H048 M12 DevOps & CI/CD audit tests.

Validates the deployment and operational configuration that lives outside the
application code: Docker, Celery tuning, deploy scripts, and the contract
between the worker's stop timeout and its longest task.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"


# --------------------------------------------------------------------------- #
# .dockerignore                                                                #
# --------------------------------------------------------------------------- #


class TestDockerignore:
    """The build context must exclude secrets, VCS history, and bloat."""

    @pytest.fixture(autouse=True)
    def _load(self):
        path = REPO / ".dockerignore"
        assert path.exists(), ".dockerignore is missing"
        self.lines = path.read_text().splitlines()

    def _ignored(self, pattern: str) -> bool:
        return any(line.strip() == pattern for line in self.lines)

    def test_git_directory_is_excluded(self):
        assert self._ignored(".git")

    def test_env_files_are_excluded(self):
        assert self._ignored(".env")

    def test_keys_directory_is_excluded(self):
        assert self._ignored("keys/")

    def test_node_modules_are_excluded(self):
        assert self._ignored("node_modules/")

    def test_venv_is_excluded(self):
        assert self._ignored("backend/.venv/")

    def test_pycache_is_excluded(self):
        assert self._ignored("__pycache__/")


# --------------------------------------------------------------------------- #
# docker-compose.yml                                                           #
# --------------------------------------------------------------------------- #


class TestDockerCompose:
    """Restart policies, resource limits, and health checks."""

    @pytest.fixture(autouse=True)
    def _load(self):
        with open(REPO / "docker-compose.yml") as f:
            self.compose = yaml.safe_load(f)
        self.services = self.compose["services"]

    @pytest.mark.parametrize("svc", ["db", "redis", "api", "worker", "beat"])
    def test_every_service_has_a_restart_policy(self, svc):
        assert "restart" in self.services[svc], f"{svc} has no restart policy"

    @pytest.mark.parametrize("svc", ["db", "redis", "api", "worker", "beat"])
    def test_every_service_has_a_memory_limit(self, svc):
        deploy = self.services[svc].get("deploy", {})
        resources = deploy.get("resources", {})
        limits = resources.get("limits", {})
        assert "memory" in limits, f"{svc} has no memory limit"

    @pytest.mark.parametrize("svc", ["db", "redis", "api", "worker"])
    def test_critical_services_have_health_checks(self, svc):
        assert "healthcheck" in self.services[svc], f"{svc} has no health check"

    def test_api_has_stop_grace_period(self):
        assert "stop_grace_period" in self.services["api"]

    def test_worker_has_stop_grace_period(self):
        assert "stop_grace_period" in self.services["worker"]

    def test_api_command_includes_graceful_shutdown(self):
        cmd = self.services["api"]["command"]
        assert "--timeout-graceful-shutdown" in cmd

    def test_worker_command_includes_concurrency(self):
        cmd = self.services["worker"]["command"]
        assert "--concurrency" in cmd

    def test_redis_has_maxmemory_policy(self):
        cmd = self.services["redis"].get("command", "")
        assert "maxmemory" in cmd


# --------------------------------------------------------------------------- #
# Celery configuration                                                         #
# --------------------------------------------------------------------------- #


class TestCeleryConfig:
    """The Celery app's conf must match production needs."""

    @pytest.fixture(autouse=True)
    def _load(self):
        from app.tasks.celery_app import celery_app
        self.conf = celery_app.conf

    def test_task_acks_late(self):
        assert self.conf.task_acks_late is True

    def test_prefetch_multiplier_is_one(self):
        assert self.conf.worker_prefetch_multiplier == 1

    def test_results_are_ignored(self):
        assert self.conf.task_ignore_result is True

    def test_serializer_is_json(self):
        assert self.conf.task_serializer == "json"

    def test_timezone_is_utc(self):
        assert self.conf.timezone == "UTC"

    def test_worker_max_tasks_per_child_is_set(self):
        assert self.conf.worker_max_tasks_per_child is not None
        assert self.conf.worker_max_tasks_per_child > 0

    def test_result_expires_is_set(self):
        assert self.conf.result_expires is not None


# --------------------------------------------------------------------------- #
# Every task has time limits                                                   #
# --------------------------------------------------------------------------- #


class TestTaskTimeLimits:
    """Every registered task must declare both soft and hard time limits."""

    @pytest.fixture(autouse=True)
    def _load(self):
        from app.tasks.celery_app import celery_app
        self.app = celery_app
        self.app.loader.import_default_modules()

    def test_every_task_has_a_soft_time_limit(self):
        missing = []
        for name, task in self.app.tasks.items():
            if not name.startswith("app.tasks."):
                continue
            if task.soft_time_limit is None:
                missing.append(name)
        assert not missing, f"tasks without soft_time_limit: {missing}"

    def test_every_task_has_a_hard_time_limit(self):
        missing = []
        for name, task in self.app.tasks.items():
            if not name.startswith("app.tasks."):
                continue
            if task.time_limit is None:
                missing.append(name)
        assert not missing, f"tasks without time_limit: {missing}"

    def test_hard_limit_exceeds_soft_limit(self):
        wrong = []
        for name, task in self.app.tasks.items():
            if not name.startswith("app.tasks."):
                continue
            if task.soft_time_limit and task.time_limit:
                if task.time_limit <= task.soft_time_limit:
                    wrong.append(name)
        assert not wrong, f"tasks where time_limit <= soft_time_limit: {wrong}"


# --------------------------------------------------------------------------- #
# Shutdown window                                                              #
# --------------------------------------------------------------------------- #


def test_shutdown_is_graceful():
    """The worker's stop timeout must clear the longest hard time_limit.

    Without this the drain is decorative on exactly the tasks worth draining:
    systemd kills the pool while Celery itself is still willing to wait. See
    deploy/systemd/pulse-worker.service for the history.
    """
    from app.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()

    max_limit = 0
    for name, task in celery_app.tasks.items():
        if name.startswith("app.tasks.") and task.time_limit:
            max_limit = max(max_limit, task.time_limit)

    unit = REPO / "deploy" / "systemd" / "pulse-worker.service"
    text = unit.read_text()
    match = re.search(r"TimeoutStopSec=(\d+)", text)
    assert match, "TimeoutStopSec not found in pulse-worker.service"

    stop_timeout = int(match.group(1))
    assert stop_timeout > max_limit, (
        f"TimeoutStopSec={stop_timeout} does not clear the longest "
        f"task hard limit ({max_limit}s) — a deploy will SIGKILL in-flight work"
    )


# --------------------------------------------------------------------------- #
# Deploy script                                                                #
# --------------------------------------------------------------------------- #


class TestDeployScript:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.script = (REPO / "deploy" / "deploy.sh").read_text()

    def test_deploy_script_uses_set_euo_pipefail(self):
        assert "set -euo pipefail" in self.script

    def test_deploy_script_runs_alembic_migration(self):
        assert "alembic" in self.script and "upgrade" in self.script

    def test_deploy_script_has_health_check(self):
        assert "/api/v1/health" in self.script

    def test_deploy_script_has_migration_lock(self):
        assert "pg_try_advisory_lock" in self.script

    def test_deploy_script_handles_migration_failure(self):
        assert "migration failed" in self.script


# --------------------------------------------------------------------------- #
# Backup script                                                                #
# --------------------------------------------------------------------------- #


class TestBackupScript:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.script = (REPO / "deploy" / "backup.sh").read_text()

    def test_backup_uses_custom_format(self):
        assert "--format=custom" in self.script

    def test_backup_verifies_dump_integrity(self):
        assert "pg_restore --list" in self.script

    def test_backup_has_retention_policy(self):
        assert "RETENTION_DAYS" in self.script

    def test_backup_checks_disk_space(self):
        assert "MIN_FREE_MB" in self.script

    def test_backup_uses_partial_then_rename(self):
        assert ".partial" in self.script


# --------------------------------------------------------------------------- #
# Health endpoint                                                              #
# --------------------------------------------------------------------------- #


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "database" in body
        assert "redis" in body

    def test_health_detail_requires_auth(self, client):
        resp = client.get("/api/v1/health/detail")
        assert resp.status_code == 401
