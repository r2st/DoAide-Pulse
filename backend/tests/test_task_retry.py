"""Verify Celery tasks have auto-retry configured for transient failures.

These are structural tests — they inspect the task decorator attributes rather
than simulating a retry, because the retry machinery belongs to Celery (tested
upstream) and what matters here is that we actually wired it in.

Two layers, and the second is what keeps the first from going stale.
``RETRYABLE_TASKS`` is a hand-written list of the six tasks that reach the
network on every run, held to the strictest policy. Below it, ``HERALD_TASKS``
walks the registry the way ``test_celery_app.py`` does, so a task added to this
tree tomorrow is covered by the floor without anybody remembering this file
exists. A hand-written list cannot catch the task that was never added to it,
and "every task is bounded" is exactly the kind of claim that quietly stops
being true one task at a time.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.tasks.autopilot_tasks import scan_all_projects, scan_project
from app.tasks.metrics_tasks import collect_all_metrics, collect_one
from app.tasks.publish_tasks import publish_due, publish_one
from tests.test_celery_app import HERALD_TASKS

RETRYABLE_TASKS = [
    publish_one,
    publish_due,
    scan_project,
    scan_all_projects,
    collect_all_metrics,
    collect_one,
]

#: Tasks that deliberately do not auto-retry, and why. Everything here is a
#: daily, idempotent housekeeping sweep or a single-row state change: the next
#: scheduled tick *is* the retry, and retrying a sweep against a database that
#: has just refused a connection adds load to the thing that is already having
#: the bad day. Anything not in this table has to carry a policy.
_NO_AUTORETRY = {
    "app.tasks.maintenance_tasks.purge_expired_tokens": "daily idempotent purge",
    "app.tasks.maintenance_tasks.purge_old_webhook_deliveries": "daily idempotent purge",
    "app.tasks.maintenance_tasks.purge_old_trigger_events": "daily idempotent purge",
    "app.tasks.maintenance_tasks.purge_old_preview_links": "daily idempotent purge",
    "app.tasks.maintenance_tasks.purge_old_llm_usage": "daily idempotent purge",
    "app.tasks.maintenance_tasks.rewrap_credentials": (
        "daily idempotent sweep; re-reads what it did not finish"
    ),
    "app.tasks.publish_tasks.cancel_publication": (
        "one row, dispatched from a request that reports its own failure"
    ),
}


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


# --------------------------------------------------------------------------- #
# The floor every task is held to, walked from the registry                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,task", HERALD_TASKS, ids=[n for n, _ in HERALD_TASKS])
def test_every_task_declares_both_time_limits(name, task):
    """A task with no soft limit cannot stop cleanly, and one with no hard limit
    cannot be stopped at all.

    The soft limit raises ``SoftTimeLimitExceeded`` *inside* the task, which is
    what lets a sweep commit what it has and report an honest count. The hard
    limit is the backstop that kills the worker if the soft one is swallowed —
    and with ``acks_late`` set, a worker killed mid-task hands its whole batch
    to the next one, so the gap between them has to be real seconds and not
    zero.
    """
    assert task.soft_time_limit, f"{name} has no soft_time_limit"
    assert task.time_limit, f"{name} has no time_limit"
    assert task.soft_time_limit < task.time_limit, (
        f"{name} has soft_time_limit {task.soft_time_limit} >= time_limit "
        f"{task.time_limit}; the soft limit has to land first or it never lands"
    )


@pytest.mark.parametrize("name,task", HERALD_TASKS, ids=[n for n, _ in HERALD_TASKS])
def test_every_task_either_retries_transient_failures_or_says_why_not(name, task):
    """``OperationalError`` is the floor: it is what a dropped Postgres
    connection raises, every task in this tree opens a session, and a connection
    that dropped once is the definition of worth trying again.

    A task that opts out belongs in ``_NO_AUTORETRY`` with a reason, which is
    the point — opting out stays possible and stops being silent.
    """
    if name in _NO_AUTORETRY:
        assert not getattr(task, "autoretry_for", ()), (
            f"{name} is listed in _NO_AUTORETRY but does declare autoretry_for; "
            "take it out of the table"
        )
        return
    assert OperationalError in getattr(task, "autoretry_for", ()), (
        f"{name} does not retry OperationalError. Add a policy, or add it to "
        "_NO_AUTORETRY with the reason it does not need one."
    )


@pytest.mark.parametrize("name,task", HERALD_TASKS, ids=[n for n, _ in HERALD_TASKS])
def test_every_retrying_task_backs_off_within_a_bound(name, task):
    """Retrying is only half a policy. Without backoff a task hammers whatever
    just failed; without jitter every task that failed in the same outage comes
    back in the same instant; without ``retry_backoff_max`` the doubling runs
    away, and without a bounded ``max_retries`` nothing ever gives up — which is
    the failure that looks like everything working until the queue is full.
    """
    if not getattr(task, "autoretry_for", ()):
        return
    assert task.retry_backoff is True, f"{name} retries without backing off"
    assert task.retry_jitter is True, f"{name} backs off without jitter"
    assert task.retry_backoff_max, f"{name} has unbounded backoff growth"
    assert 1 <= task.max_retries <= 5, (
        f"{name} has max_retries={task.max_retries}; a task that never gives up "
        "fills the queue instead of failing"
    )
