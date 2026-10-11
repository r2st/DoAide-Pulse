"""Docker and docker-compose health checks are correctly configured.

A container without a health check can sit in "running" state while the process
inside it is broken. docker-compose's ``depends_on: condition: service_healthy``
only works when the depended-on service has a health check that actually probes
the thing the downstream needs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(yaml is None, reason="pyyaml not installed")
class TestDockerComposeHealthChecks:
    @pytest.fixture(autouse=True)
    def _load_compose(self) -> None:
        self.compose = yaml.safe_load(
            (REPO_ROOT / "docker-compose.yml").read_text()
        )

    def test_every_service_has_a_healthcheck(self) -> None:
        for name, service in self.compose["services"].items():
            assert "healthcheck" in service, (
                f"service '{name}' has no healthcheck"
            )

    def test_api_healthcheck_probes_the_health_endpoint(self) -> None:
        test = self.compose["services"]["api"]["healthcheck"]["test"]
        joined = " ".join(test) if isinstance(test, list) else test
        assert "health" in joined

    def test_api_has_start_period(self) -> None:
        hc = self.compose["services"]["api"]["healthcheck"]
        assert "start_period" in hc

    def test_api_has_stop_grace_period(self) -> None:
        api = self.compose["services"]["api"]
        assert "stop_grace_period" in api

    def test_db_healthcheck_uses_pg_isready(self) -> None:
        test = self.compose["services"]["db"]["healthcheck"]["test"]
        joined = " ".join(test) if isinstance(test, list) else test
        assert "pg_isready" in joined

    def test_redis_healthcheck_uses_ping(self) -> None:
        test = self.compose["services"]["redis"]["healthcheck"]["test"]
        joined = " ".join(test) if isinstance(test, list) else test
        assert "ping" in joined


class TestDockerfile:
    @pytest.fixture(autouse=True)
    def _load_dockerfile(self) -> None:
        self.dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text()

    def test_has_healthcheck(self) -> None:
        assert "HEALTHCHECK" in self.dockerfile

    def test_has_stopsignal(self) -> None:
        assert "STOPSIGNAL SIGTERM" in self.dockerfile

    def test_runs_as_non_root(self) -> None:
        assert "USER" in self.dockerfile
        assert re.search(r"^USER\s+\S+", self.dockerfile, re.MULTILINE)

    def test_graceful_shutdown_timeout(self) -> None:
        assert "--timeout-graceful-shutdown" in self.dockerfile
