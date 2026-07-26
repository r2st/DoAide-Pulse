"""Celery application + beat schedule."""
from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "herald",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.publish_tasks",
        "app.tasks.autopilot_tasks",
        "app.tasks.metrics_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Nothing ever reads a task result — every task records its outcome in
    # Postgres. Ignoring results keeps apply_async from touching the result
    # store, which otherwise blocks the caller for ~20s reconnecting whenever
    # Redis is unavailable.
    task_ignore_result=True,
    # Fail fast when the broker is unreachable instead of blocking the caller.
    # The API dispatches publishes with .delay() from inside a request and falls
    # back to running them inline (see routers/content._dispatch) — that
    # fallback only works if the publish raises promptly rather than retrying
    # for minutes. Workers are unaffected; they reconnect via broker_pool_limit.
    broker_connection_retry_on_startup=False,
    # NB: 0 (and None) mean "retry forever" in Celery — 1 is the fail-fast value.
    broker_connection_max_retries=1,
    broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 2},
    task_publish_retry=False,
)

# Periodic jobs.
#
# Cadences differ by how fast the underlying thing moves: a scheduled post is
# due to the minute, repos ship a few times a day at most, and view counts move
# slowly enough that polling them more than four times a day is just API quota
# spent on noise.
celery_app.conf.beat_schedule = {
    "publish-due-content": {
        "task": "app.tasks.publish_tasks.publish_due",
        "schedule": float(settings.publish_scan_interval_seconds),
    },
    "scan-project-repos": {
        "task": "app.tasks.autopilot_tasks.scan_all_projects",
        "schedule": float(settings.autopilot_scan_interval_seconds),
    },
    "collect-metrics": {
        "task": "app.tasks.metrics_tasks.collect_all_metrics",
        "schedule": float(settings.metrics_scan_interval_seconds),
    },
}
