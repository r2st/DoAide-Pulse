"""What a task run leaves behind in the log.

The API side of Herald is traceable: every request is given an id, the id is
returned in ``X-Request-ID``, and :class:`app.logging_config.RequestIDFilter`
stamps it onto every line written while that request is being served — including
lines from services that have no idea a request exists.

The trail stops at ``.delay()``. Herald's product runs in the worker: a request
that queues a publish returns in milliseconds and the work happens somewhere
else, minutes later, in a different process. Those lines were stamped ``-``,
which is exactly true and operationally useless — the question being asked is
always "the user says their post never went out, what happened", and the answer
was in a log line with nothing linking it to the request that started it.

Three things are added here, all of them reporting:

* **the id crosses the broker.** :func:`_stamp_request_id` puts the current id
  into the task message on the way out and :func:`_task_started` puts it back
  into the logging context on the way in, so one ``grep`` over the journal
  returns the request *and* the work it caused, across two processes and
  however much time passed between them;
* **every run is timed.** One INFO line per task with a duration, which is the
  only record of how long anything takes here — there is no metrics backend on
  this box, and "publishing got slow" is otherwise a thing somebody notices in
  the UI;
* **failures come with their context.** The task, its arguments, which attempt
  this was, and whether the failure is worth retrying at all — see
  :mod:`app.services.error_class`. A traceback says what broke. It does not say
  whether waiting will fix it, and that is the decision an operator is actually
  making at the time they read it.

None of it changes what a task *does*. Signal receivers cannot: a raising
receiver would take the task down with it, so everything below is written to be
unable to raise, and the classification is reported rather than acted on.

Registered by importing this module, which :mod:`app.tasks.celery_app` does at
the bottom of itself — so the receivers exist in the worker, in beat, and in the
API process that dispatches, which is the only one of the three that can stamp
an outgoing message with a request id.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from celery.signals import (
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_success,
)

from app.logging_config import request_id_var
from app.services.error_class import classify

logger = logging.getLogger(__name__)

#: The message header the request id travels in. Namespaced because it shares a
#: dict with Celery's own protocol fields, and a collision there is not a bug
#: that announces itself.
REQUEST_ID_HEADER = "herald_request_id"

#: How much of a task's arguments is worth putting in a failure line. The
#: arguments here are row ids, so this is generous for the real cases and a cap
#: on the one that isn't — a task called with something unexpectedly large
#: should not write it to the journal once per attempt.
MAX_ARGS_CHARS = 200

#: Per-run state, keyed by task id: when it started and the context token that
#: undoes its request id. Populated in :func:`_task_started` and removed in
#: :func:`_task_finished`, which Celery pairs — ``task_prerun`` is sent inside
#: the tracer's ``try`` and the terminal signals from its ``finally``.
#:
#: A miss is survivable by construction: every reader below falls back rather
#: than raising, so a run whose start was never recorded is logged without a
#: duration instead of not being logged.
_RUNS: dict[str, tuple[float, Any]] = {}


@before_task_publish.connect
def _stamp_request_id(headers: dict[str, Any] | None = None, **_kwargs: object) -> None:
    """Carry the current request id into the message being published.

    Runs in whichever process is dispatching: the API inside a request, or a
    worker inside a sweep that is fanning out per-item tasks. Both cases are the
    same statement — "the work this message describes was caused by that" — and
    the second is what keeps a chain of tasks under one id instead of starting a
    new trail at every hop.

    Nothing is stamped when there is no id to stamp. ``-`` is the contextvar's
    default and means "not part of a request"; writing it into the header would
    make the worker unable to tell a beat-scheduled task from one that lost its
    id, and the worker's fallback for the former is better than the string ``-``.
    """
    if headers is None:  # protocol 1, or a caller that passed nothing
        return
    current = request_id_var.get()
    if current and current != "-":
        headers[REQUEST_ID_HEADER] = current


@task_prerun.connect
def _task_started(
    task_id: str | None = None, task: Any = None, **_kwargs: object
) -> None:
    """Adopt the dispatcher's request id, or mint one from the task id.

    The fallback matters as much as the propagation. Most work here is started
    by beat, which has no request behind it, and those runs still need every
    line they write to be greppable as one unit — a publish sweep logs from
    four modules across a hundred rows. The task id is already unique and
    already in the "task finished" line below, so its first twelve characters
    are the natural id, and twelve is what
    :class:`app.main.RequestIDMiddleware` mints for a request too, which keeps
    the column one width in the journal.
    """
    inherited = getattr(getattr(task, "request", None), REQUEST_ID_HEADER, None)
    resolved = inherited if isinstance(inherited, str) and inherited else _short(task_id)
    token = request_id_var.set(resolved)
    if task_id is not None:
        _RUNS[task_id] = (time.monotonic(), token)
    logger.debug("task started: %s [task_id=%s]", _name(task), task_id)


@task_success.connect
def _task_succeeded(sender: Any = None, **_kwargs: object) -> None:
    """One INFO line per successful run, with how long it took.

    On ``task_success`` rather than ``task_postrun`` so a failure is not
    reported twice — :func:`_task_failed` already writes the interesting line
    for that path, and a second "finished in 0.4s" underneath it says nothing
    the first did not.

    The cleanup is *not* here for the same reason. See :func:`_task_finished`.
    """
    task_id = getattr(getattr(sender, "request", None), "id", None)
    started = _RUNS.get(task_id or "", (None, None))[0]
    if started is None:
        logger.info("task ok: %s [task_id=%s]", _name(sender), task_id)
        return
    logger.info(
        "task ok: %s in %.3fs [task_id=%s]",
        _name(sender),
        time.monotonic() - started,
        task_id,
    )


@task_failure.connect
def _task_failed(
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: tuple[object, ...] | None = None,
    kwargs: dict[str, object] | None = None,
    sender: Any = None,
    **_extra: object,
) -> None:
    """The line that has to be enough on its own.

    "Enough" is: which task, on what, how long it had been running, which
    attempt this was, and whether another one could plausibly work. Without the
    arguments the failure cannot be tied to a row; without the retry count a
    permanent failure is indistinguishable from the first of four attempts;
    without the classification the reader has to know Herald's exception
    hierarchy to tell "come back in an hour" from "this will never succeed".

    ``exc_info`` carries the traceback, so this is one log *event* rather than a
    summary line that has to be joined against a stack trace somewhere above it.
    """
    duration = _elapsed(task_id)
    retries = getattr(getattr(sender, "request", None), "retries", 0) or 0
    logger.error(
        "task failed: %s %s attempt=%d%s args=%s kwargs=%s [task_id=%s]",
        _name(sender),
        _describe(exception),
        retries + 1,
        "" if duration is None else f" after {duration:.3f}s",
        _clip(args or ()),
        _clip(kwargs or {}),
        task_id,
        exc_info=exception,
    )


@task_retry.connect
def _task_retrying(
    request: Any = None, reason: Any = None, sender: Any = None, **_extra: object
) -> None:
    """A retry is a warning, not an error — but a silent one is neither.

    Celery's own retry line is at INFO and says only that it is retrying. What
    makes this worth a line of Herald's own is the classification: a task
    retrying a *permanent* failure is spending its whole budget on an outcome
    that cannot change, which is invisible in the individual attempts and
    obvious the moment the class is printed beside them.
    """
    task_id = getattr(request, "id", None)
    logger.warning(
        "task retrying: %s %s attempt=%d [task_id=%s]",
        _name(sender),
        _describe(reason if isinstance(reason, BaseException) else None),
        (getattr(request, "retries", 0) or 0) + 1,
        task_id,
    )


@task_postrun.connect
def _task_finished(task_id: str | None = None, **_kwargs: object) -> None:
    """Drop the run's state and put the logging context back as it was.

    Last of the terminal signals, which is what makes the two above safe to
    write as readers: Celery's tracer sends ``task_success`` and ``task_failure``
    from inside its ``try`` and ``task_postrun`` from the ``finally`` beneath it,
    so the timing entry this removes is still present when either of them reads
    it, and is gone before the process picks up its next task either way.

    Resetting the contextvar is not housekeeping. A worker process is reused for
    the next task, and prefork pools do not start a fresh context per run — a
    token left unreset leaves the finished task's id stamped on whatever the
    process does next, including on lines written between tasks, which is worse
    than no id at all because it is a wrong one that looks right.
    """
    _, token = _RUNS.pop(task_id or "", (None, None))
    if token is not None:
        try:
            request_id_var.reset(token)
        except ValueError:  # pragma: no cover - set in another context
            # The token belongs to a context this call cannot reset from. The
            # id is wrong from here on rather than absent, so say so explicitly.
            request_id_var.set("-")


def _elapsed(task_id: str | None) -> float | None:
    """How long the run identified by *task_id* has been going, if it is known."""
    started = _RUNS.get(task_id or "", (None, None))[0]
    return None if started is None else time.monotonic() - started


def _describe(exc: BaseException | None) -> str:
    """``class=… type=…`` for *exc*, or a stand-in when there is no exception.

    Celery can report a retry with a reason that is not an exception at all
    (a plain ``Retry`` message), and a formatter that assumed otherwise would
    raise inside a signal receiver — which is the one place an exception is
    least welcome.
    """
    if exc is None:
        return "class=unknown type=none"
    return f"class={classify(exc)} type={type(exc).__name__}"


def _name(task: Any) -> str:
    """A task's registered name, for a sender that may be anything."""
    return getattr(task, "name", None) or repr(task)


def _short(task_id: str | None) -> str:
    """The first twelve characters of a task id, or the no-request marker."""
    return task_id[:12] if task_id else "-"


def _clip(value: object) -> str:
    """*value* rendered for a log line and bounded to :data:`MAX_ARGS_CHARS`.

    ``repr`` rather than ``str`` so the difference between ``7`` and ``"7"``
    survives — that difference is occasionally the bug. Failures of ``repr``
    itself are swallowed: an object whose ``__repr__`` raises must not be able
    to convert a logged failure into a lost one.
    """
    try:
        rendered = repr(value)
    except Exception:  # pragma: no cover - defensive
        return "<unrepresentable>"
    if len(rendered) <= MAX_ARGS_CHARS:
        return rendered
    return rendered[: MAX_ARGS_CHARS - 1] + "…"
