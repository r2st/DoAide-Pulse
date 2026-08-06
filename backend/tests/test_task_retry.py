"""Verify Celery tasks have auto-retry configured for transient failures.

These are structural tests — they inspect the task decorator attributes rather
than simulating a retry, because the retry machinery belongs to Celery (tested
upstream) and what matters here is that we actually wired it in.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.tasks.autopilot_tasks import scan_all_projects, scan_project
from app.tasks.metrics_tasks import collect_all_metrics, collect_one
from app.tasks.publish_tasks import publish_due, publish_one

RETRYABLE_TASKS = [
    publish_one,
    publish_due,
    scan_project,
    scan_all_projects,
    collect_all_metrics,
    collect_one,
]


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_autoretry_includes_operational_error(task):
    """OperationalError (DB connection lost) triggers a retry."""
    assert OperationalError in task.autoretry_for


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_autoretry_includes_connection_error(task):
    assert ConnectionError in task.autoretry_for


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_autoretry_includes_os_error(task):
    assert OSError in task.autoretry_for


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_retry_backoff_enabled(task):
    assert task.retry_backoff is True


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_retry_jitter_enabled(task):
    assert task.retry_jitter is True


@pytest.mark.parametrize("task", RETRYABLE_TASKS, ids=lambda t: t.name)
def test_max_retries_is_bounded(task):
    assert 1 <= task.max_retries <= 5
