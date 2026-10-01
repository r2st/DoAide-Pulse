"""A task that died should not have to be re-run to find out why.

Nothing on this box collects metrics and nothing aggregates errors; the journal
is the whole of the operational record, and what it held for a failed task was
Celery's own line — the exception, a traceback, and the task's name. Missing
from that: *which row* (so the failure cannot be tied to the user reporting it),
*which attempt* (so a permanent failure looks the same as the first of four),
*how long it ran* (so nothing distinguishes an instant refusal from a timeout),
and *whether another attempt could plausibly work* — which is the only one of
the four that the reader is actually deciding about.

Duration is here for the same reason and not only for failures. There is no
other record of how long anything takes in this system, so "publishing got
slow" was a thing somebody noticed in the UI rather than in the logs.

The receivers are held to one more rule throughout: they cannot raise. A signal
receiver that throws takes the task down with it, which would mean a bug in the
logging turning a recorded failure into an unrecorded one — strictly worse than
the gap it was written to close.
"""
from __future__ import annotations

import logging

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded

from app.logging_config import request_id_var
from app.services.publishers.base import CredentialError
from app.tasks import observability


@pytest.fixture(autouse=True)
def clean_slate():
    """Every test here starts a run and none of them finishes one.

    ``_task_started`` sets the request-id contextvar, and only ``_task_finished``
    puts it back — so without this the id of one test's imaginary task is
    stamped on the next test's log lines, which is the same leak the receivers
    exist to prevent and would be invisible from inside a test that never reads
    it.
    """
    observability._RUNS.clear()
    token = request_id_var.set("-")
    try:
        yield
    finally:
        request_id_var.reset(token)
        observability._RUNS.clear()


class _Request:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _Task:
    def __init__(self, name: str = "app.tasks.publish_tasks.publish_one", **req: object):
        self.name = name
        self.request = _Request(**req)


def _run(task_id: str, task: _Task | None = None) -> _Task:
    """Start a run the way ``task_prerun`` does, and hand back the task."""
    task = task or _Task()
    observability._task_started(task_id=task_id, task=task)
    return task


# --- what a successful run leaves behind ------------------------------------


def test_a_finished_task_reports_how_long_it_took(caplog):
    """The only timing signal in the system, so it is on the ordinary path."""
    task = _run("run-1")
    task.request.id = "run-1"

    with caplog.at_level(logging.INFO, logger="app.tasks.observability"):
        observability._task_succeeded(sender=task)

    line = caplog.text
    assert "task ok: app.tasks.publish_tasks.publish_one in " in line
    assert "task_id=run-1" in line


def test_a_run_whose_start_was_never_seen_is_still_reported(caplog):
    """Without the fallback, a receiver connected mid-flight would raise inside
    a signal — and the run it could not time would be the run nobody hears
    about at all, rather than the one logged without a duration."""
    task = _Task()
    task.request.id = "never-started"

    with caplog.at_level(logging.INFO, logger="app.tasks.observability"):
        observability._task_succeeded(sender=task)

    assert "task ok: app.tasks.publish_tasks.publish_one [task_id=never-started]" in (
        caplog.text
    )


def test_a_finished_run_does_not_stay_in_memory():
    """``_RUNS`` is per-run state in a process that runs tasks forever."""
    _run("run-1")
    assert observability._RUNS

    observability._task_finished(task_id="run-1")

    assert observability._RUNS == {}


# --- what a failed run leaves behind ----------------------------------------


def test_a_failure_line_carries_everything_needed_to_find_the_row(caplog):
    task = _run("run-2")

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-2",
            exception=CredentialError("devto rejected the API key"),
            args=(4162,),
            kwargs={"force": True},
            sender=task,
        )

    line = caplog.text
    assert "task failed: app.tasks.publish_tasks.publish_one" in line
    # Which row.
    assert "args=(4162,)" in line
    assert "kwargs={'force': True}" in line
    # Which attempt, counted the way a person counts them — the first is 1.
    assert "attempt=1" in line
    # How long it had been going.
    assert " after " in line
    assert "task_id=run-2" in line


def test_a_failure_line_says_whether_waiting_would_help(caplog):
    """The decision the reader is making. See ``app.services.error_class``."""
    permanent = _run("run-3")
    retryable = _run("run-4")

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-3", exception=CredentialError("token rejected"), sender=permanent
        )
        observability._task_failed(
            task_id="run-4", exception=SoftTimeLimitExceeded(), sender=retryable
        )

    assert "class=permanent type=CredentialError" in caplog.text
    assert "class=retryable type=SoftTimeLimitExceeded" in caplog.text


def test_a_failure_line_counts_the_attempt_from_celery_s_own_retry_count(caplog):
    """A fourth attempt failing is a different event from a first one failing,
    and the two were indistinguishable in the log."""
    task = _run("run-5", _Task(retries=3))

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-5", exception=ValueError("nope"), sender=task
        )

    assert "attempt=4" in caplog.text


def test_a_failure_carries_its_traceback_as_one_event(caplog):
    """``exc_info`` rather than a summary line beside a stack trace: journald
    interleaves processes, and two lines that have to be read together are two
    lines that eventually are not."""
    task = _run("run-6")
    raised = CredentialError("expired")
    try:
        raise raised
    except CredentialError as exc:
        with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
            observability._task_failed(task_id="run-6", exception=exc, sender=task)

    record = caplog.records[-1]
    assert record.exc_info is not None
    assert record.exc_info[1] is raised


# --- what a retry leaves behind ---------------------------------------------


def test_a_retry_is_logged_with_the_class_it_is_retrying(caplog):
    """A task retrying a permanent failure spends its whole budget on an
    outcome that cannot change. Invisible in the individual attempts; obvious
    the moment the class is printed beside them."""
    request = _Request(id="run-7", retries=1)

    with caplog.at_level(logging.WARNING, logger="app.tasks.observability"):
        observability._task_retrying(
            request=request, reason=CredentialError("still rejected"), sender=_Task()
        )

    line = caplog.text
    assert "task retrying: app.tasks.publish_tasks.publish_one" in line
    assert "class=permanent type=CredentialError" in line
    assert "attempt=2" in line


def test_a_retry_whose_reason_is_not_an_exception_is_survived(caplog):
    """Celery can report the reason as a bare ``Retry`` message.

    A formatter that assumed an exception would raise inside a signal receiver —
    the one place an exception is least welcome.
    """
    with caplog.at_level(logging.WARNING, logger="app.tasks.observability"):
        observability._task_retrying(
            request=_Request(id="run-8", retries=0), reason="in 30s", sender=_Task()
        )

    assert "class=unknown type=none" in caplog.text
    # A `Retry` *is* an exception, and classifies as the unknown it is.
    with caplog.at_level(logging.WARNING, logger="app.tasks.observability"):
        observability._task_retrying(
            request=_Request(id="run-9", retries=0), reason=Retry(), sender=_Task()
        )
    assert "type=Retry" in caplog.text


# --- the arguments, bounded -------------------------------------------------


def test_an_oversized_argument_does_not_go_into_the_journal_whole(caplog):
    """Pulse's tasks take row ids, so the cap is generous for every real call
    and a bound on the one that isn't — a failure is logged once per attempt,
    and an unbounded argument is written to disk each time."""
    task = _run("run-10")

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-10",
            exception=ValueError("x"),
            args=("y" * 5000,),
            sender=task,
        )

    rendered = next(
        part for part in caplog.text.split() if part.startswith("args=")
    )
    assert len(rendered) <= observability.MAX_ARGS_CHARS + len("args=")
    assert rendered.endswith("…")


def test_an_argument_whose_repr_explodes_does_not_lose_the_failure(caplog):
    """The failure being logged is the important one; a bad ``__repr__`` in a
    task argument must not be able to replace it with its own."""

    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("no")

    task = _run("run-11")

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-11",
            exception=ValueError("the real problem"),
            args=(Hostile(),),
            sender=task,
        )

    assert "<unrepresentable>" in caplog.text
    assert "type=ValueError" in caplog.text


def test_the_arguments_are_shown_as_they_were_typed(caplog):
    """``repr`` rather than ``str``: ``7`` and ``"7"`` are occasionally the bug,
    and a publication id that arrived as a string is exactly that bug."""
    task = _run("run-12")

    with caplog.at_level(logging.ERROR, logger="app.tasks.observability"):
        observability._task_failed(
            task_id="run-12", exception=ValueError("x"), args=("7",), sender=task
        )

    assert "args=('7',)" in caplog.text


# --- nothing here may raise -------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: observability._task_started(task_id=None, task=None),
        lambda: observability._task_succeeded(sender=None),
        lambda: observability._task_failed(task_id=None, exception=None, sender=None),
        lambda: observability._task_retrying(request=None, reason=None, sender=None),
        lambda: observability._task_finished(task_id=None),
    ],
    ids=["started", "succeeded", "failed", "retrying", "finished"],
)
def test_a_receiver_handed_nothing_at_all_still_does_not_raise(call):
    """Signals arrive with whatever the caller had. A receiver that throws takes
    the task with it, so every one of these degrades to a poorer line rather
    than to an exception."""
    call()
