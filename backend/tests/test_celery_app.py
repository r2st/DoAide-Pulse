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
from app.tasks import celery_app as celery_module
from app.tasks.celery_app import celery_app

HERALD_TASKS = [
    (name, obj)
    for name, obj in sorted(celery_app.tasks.items())
    if name.startswith("app.tasks.")
]


def test_every_scheduled_task_name_is_a_real_registered_task():
    """A beat entry that names a task nothing defines fails silently forever."""
    registered = set(celery_app.tasks.keys())
    for job_name, entry in celery_app.conf.beat_schedule.items():
        assert entry["task"] in registered, (
            f"beat job {job_name!r} names {entry['task']!r}, which no "
            "@task has registered"
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


def test_every_module_that_defines_a_task_is_in_include():
    """The other direction: a new task module that nobody added to ``include=``
    is registered in every test (they import it) and invisible to a worker."""
    defining = {name.rsplit(".", 1)[0] for name, _ in HERALD_TASKS}
    assert defining <= set(celery_app.conf.include), defining - set(
        celery_app.conf.include
    )


def test_the_typed_task_decorator_is_celery_s_own():
    """``task`` exists to give ``celery_app.task`` a signature a type checker can
    read — Celery ships no ``py.typed``, so without it ``.delay`` resolves to
    nothing at every dispatch site. It is an annotation and nothing else: the
    moment it becomes a real wrapper it is a second registration path, with its
    own bugs, in front of the one Celery documents.

    ``is`` would not do: attribute access builds a fresh bound method each time,
    so the invariant is that it is Celery's own function bound to this app.

    Read through ``vars`` rather than the imported name, because the annotation
    under test deliberately exposes nothing but ``__call__`` — the question here
    is what the module really holds, not what it advertises."""
    alias = vars(celery_module)["task"]
    assert alias.__func__ is celery_app.task.__func__
    assert alias.__self__ is celery_app


def test_every_registered_task_offers_both_ways_to_run_it():
    """What the annotation on ``task`` promises, checked against the objects
    Celery actually built. Every dispatch site in the tree uses both arms: it
    calls ``.delay`` first and falls back to calling the task inline when the
    broker refuses, so a task missing either one breaks the fallback rather than
    the happy path — the harder failure to see."""
    assert HERALD_TASKS, "no Herald tasks registered; the imports above went stale"
    for name, obj in HERALD_TASKS:
        assert callable(getattr(obj, "delay", None)), f"{name} cannot be dispatched"
        assert callable(obj), f"{name} cannot be run inline"
        assert getattr(obj, "name", None) == name
