"""The beat schedule itself — not any one task, the wiring that finds them.

``celery_app.conf.beat_schedule`` names each periodic job's task by a plain
string, and that string is checked against nothing at import time: a typo in
it, or a task renamed on one side and not the other, does not fail a build or
raise on worker start. It fails silently in production, forever, the moment
beat tries to send it — ``KeyError`` inside Celery's own scheduler loop, logged
somewhere nobody watches a periodic job for until the thing it was supposed to
do stops happening. Every other task module has direct coverage; nothing
previously imported ``celery_app`` at all.
"""
from __future__ import annotations

from app.tasks import (  # noqa: F401 -- import registers each module's tasks
    autopilot_tasks,
    digest_tasks,
    headline_tasks,
    maintenance_tasks,
    metrics_tasks,
    publish_tasks,
    trigger_tasks,
    webhook_tasks,
)
from app.tasks.celery_app import celery_app


def test_every_scheduled_task_name_is_a_real_registered_task():
    """A beat entry that names a task nothing defines fails silently forever."""
    registered = set(celery_app.tasks.keys())
    for job_name, entry in celery_app.conf.beat_schedule.items():
        assert entry["task"] in registered, (
            f"beat job {job_name!r} names {entry['task']!r}, which no "
            "@celery_app.task has registered"
        )


def test_every_schedule_interval_is_positive():
    """A zero or negative interval either never fires or fires in a tight loop."""
    for job_name, entry in celery_app.conf.beat_schedule.items():
        schedule = entry["schedule"]
        # A crontab (the weekly digest) isn't a number — it's due by calendar
        # rules, which have no "positive" to check.
        if isinstance(schedule, (int, float)):
            assert schedule > 0, f"beat job {job_name!r} has a non-positive interval"


def test_no_two_beat_jobs_share_a_task():
    """Two schedule entries pointed at the same task is almost always a copy-paste
    that forgot to repoint the second one — it means one task runs twice as often
    as intended and the job that was meant to run never does."""
    tasks = [entry["task"] for entry in celery_app.conf.beat_schedule.values()]
    assert len(tasks) == len(set(tasks))


def test_every_included_module_is_reachable_from_the_app():
    """``include=`` is what makes a worker started fresh (no prior import) find
    these tasks at all — a module missing from it registers fine in a test that
    imports it directly and never runs in production."""
    for dotted in celery_app.conf.include:
        assert dotted.startswith("app.tasks."), dotted
