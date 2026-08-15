"""The trail from a request to the work it caused, across two processes.

Herald's API is already traceable in-process: ``RequestIDMiddleware`` mints an
id, returns it as ``X-Request-ID``, and ``RequestIDFilter`` stamps it on every
line written while that request is being served. The trail ended at
``.delay()`` — the request returned in milliseconds and the publishing happened
in a worker, minutes later, under the id ``-``.

That is the gap this covers, and the question it exists to answer is always the
same one: *a user says their post never went out — what happened?* Answering it
means finding the lines the worker wrote, and before this there was nothing in
them tying the work back to the request that asked for it.

Two halves, and both have to hold or the id is worse than useless:

* the id has to **travel** — into the message on the way out, back out of it on
  the way in, and onward through any task a task dispatches;
* it has to **stop**. A worker process is reused, and prefork does not hand each
  run a fresh context. An id left set is stamped on whatever the process does
  next, which is a wrong answer that looks exactly like a right one.
"""
from __future__ import annotations

import logging
from io import StringIO
from weakref import ref

import pytest
from celery.signals import (
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_success,
)

from app.logging_config import configure_logging, request_id_var
from app.tasks import observability
from app.tasks.celery_app import celery_app
from app.tasks.observability import REQUEST_ID_HEADER

#: A task that exists only to be traced. Named outside the ``app.tasks.``
#: namespace on purpose: ``tests/test_celery_app.py`` sweeps the registry for
#: that prefix and holds every match to Herald's own invariants, and this is not
#: one of Herald's tasks.
TASK_NAME = "tests.observability.noisy"

logger = logging.getLogger("app.demo")


@celery_app.task(name=TASK_NAME)
def noisy(message: str = "working") -> str:
    """Log one line from inside a task run, and report the id it carried."""
    logger.info(message)
    return request_id_var.get()


@pytest.fixture(autouse=True)
def clean_slate():
    """No run state and no request id leaking between these tests."""
    observability._RUNS.clear()
    token = request_id_var.set("-")
    try:
        yield
    finally:
        request_id_var.reset(token)
        observability._RUNS.clear()


@pytest.fixture
def pristine_logging():
    """Give a test the root logger to itself, and give it back afterwards.

    pytest's own capture handler lives on the root logger, so a test that left
    ``configure_logging``'s work in place would take the rest of the suite's
    output with it. The same fixture as ``tests/test_logging_config.py``'s, kept
    local because a fixture shared between two files belongs in ``conftest`` or
    in neither, and this one is four lines of bookkeeping either way.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_demo = logging.getLogger("app.demo").level
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("app.demo").setLevel(saved_demo)


class _Request:
    """The subset of ``task.request`` these receivers read."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _Task:
    def __init__(self, name: str = TASK_NAME, **request: object) -> None:
        self.name = name
        self.request = _Request(**request)


# --- the id leaves ----------------------------------------------------------


def test_the_dispatching_request_s_id_goes_into_the_message():
    """``before_task_publish`` is the only hook that runs in the *caller*."""
    request_id_var.set("abc123def456")
    headers: dict[str, object] = {"task": TASK_NAME}

    before_task_publish.send(sender=TASK_NAME, headers=headers, body=None)

    assert headers[REQUEST_ID_HEADER] == "abc123def456"


def test_a_dispatch_with_no_request_behind_it_stamps_nothing():
    """``-`` is the contextvar's "not in a request" default.

    Writing it into the header would tell the worker "an id was sent and it was
    nothing", which is not the same as "none was sent" — and the worker's
    fallback for the second case is a real, greppable id derived from the task.
    """
    headers: dict[str, object] = {"task": TASK_NAME}

    before_task_publish.send(sender=TASK_NAME, headers=headers, body=None)

    assert REQUEST_ID_HEADER not in headers


def test_a_publish_with_no_headers_at_all_is_survived():
    """Protocol 1 carries everything in the body and passes no headers dict."""
    request_id_var.set("abc123def456")

    before_task_publish.send(sender=TASK_NAME, headers=None, body=None)  # must not raise


def test_a_task_that_dispatches_a_task_keeps_the_one_id():
    """The fan-out case: a sweep queues one message per row.

    Without this, a publish sweep started by a request would break the chain at
    its first hop and every row it dispatched would get a fresh trail — which is
    the shape of nearly all of Herald's work.
    """
    sweep = _Task(**{REQUEST_ID_HEADER: "req-from-api"})
    observability._task_started(task_id="sweep-id", task=sweep)
    try:
        headers: dict[str, object] = {}
        before_task_publish.send(sender=TASK_NAME, headers=headers, body=None)
    finally:
        observability._task_finished(task_id="sweep-id")

    assert headers[REQUEST_ID_HEADER] == "req-from-api"


# --- the id arrives ---------------------------------------------------------


def test_the_worker_adopts_the_id_the_message_carried():
    task = _Task(**{REQUEST_ID_HEADER: "abc123def456"})

    observability._task_started(task_id="task-uuid-here", task=task)

    assert request_id_var.get() == "abc123def456"


def test_a_task_nobody_dispatched_still_gets_a_greppable_id():
    """Beat's tasks have no request behind them and still write a hundred lines.

    Twelve characters of the task id: unique already, present in full in the
    task's own start and finish lines, and the same width as the id
    ``RequestIDMiddleware`` mints — so the column in the journal stays one width
    whichever process wrote the line.
    """
    observability._task_started(task_id="0123456789abcdef0123", task=_Task())

    assert request_id_var.get() == "0123456789ab"


def test_an_empty_header_does_not_beat_the_fallback():
    """A header present but blank is not an id; the fallback is better."""
    observability._task_started(
        task_id="0123456789abcdef", task=_Task(**{REQUEST_ID_HEADER: ""})
    )

    assert request_id_var.get() == "0123456789ab"


def test_a_task_with_no_id_at_all_leaves_the_marker():
    observability._task_started(task_id=None, task=_Task())

    assert request_id_var.get() == "-"


# --- the id stops -----------------------------------------------------------


def test_the_id_does_not_outlive_the_task():
    """The reason ``task_postrun`` resets rather than merely forgetting.

    A prefork child runs the next task in the same context. Left set, this id
    would be stamped on the next task's lines *and* on anything the process logs
    between tasks — a wrong id, indistinguishable from a right one, on work that
    has nothing to do with the request it names.
    """
    observability._task_started(task_id="task-uuid-here", task=_Task())
    assert request_id_var.get() == "task-uuid-he"

    observability._task_finished(task_id="task-uuid-here")

    assert request_id_var.get() == "-"


def test_a_task_that_failed_still_gives_the_id_back():
    """``task_postrun`` is sent from the tracer's ``finally``, so this holds on
    the failing path too — which is the path where the next task's lines being
    mislabelled would matter most."""
    observability._task_started(task_id="doomed", task=_Task())
    observability._task_failed(
        task_id="doomed", exception=ValueError("nope"), sender=_Task()
    )
    observability._task_finished(task_id="doomed")

    assert request_id_var.get() == "-"


def test_finishing_a_task_that_was_never_started_is_not_an_error():
    """A receiver connected mid-flight, or a signal Celery sent alone."""
    observability._task_finished(task_id="never-seen")

    assert request_id_var.get() == "-"


# --- end to end -------------------------------------------------------------


def test_a_line_written_inside_a_task_carries_that_task_s_id(pristine_logging):
    """The whole point, exercised through Celery's own tracer.

    ``.apply()`` runs the task the way the worker does — pushing a request,
    sending ``task_prerun``, running the body, sending the terminal signals — so
    what this asserts is the wiring, not the receivers.
    """
    stream = _herald_stream(pristine_logging)

    result = noisy.apply(args=("published to devto",), task_id="feedfacecafe0000")

    assert result.get() == "feedfacecafe"
    assert "[feedfacecafe] app.demo: published to devto" in stream.getvalue()


def test_a_task_run_inside_a_request_hands_the_request_its_id_back(pristine_logging):
    """The inline fallback: every dispatch site in the tree calls ``.delay()``
    and runs the task in-process when the broker refuses, so a task run happens
    *inside* a request that is still being served. The task's own lines belong
    to the task — it is a real run, with a real task id — but the request has
    more to do afterwards, and its remaining lines must not inherit the id of
    the work it dispatched partway through."""
    _herald_stream(pristine_logging)
    request_id_var.set("aaaabbbbcccc")

    noisy.apply(args=("inline fallback",), task_id="feedfacecafe0000")

    assert request_id_var.get() == "aaaabbbbcccc"


# --- the wiring itself ------------------------------------------------------


@pytest.mark.parametrize(
    ("signal", "receiver"),
    [
        (before_task_publish, observability._stamp_request_id),
        (task_prerun, observability._task_started),
        (task_success, observability._task_succeeded),
        (task_failure, observability._task_failed),
        (task_retry, observability._task_retrying),
        (task_postrun, observability._task_finished),
    ],
    ids=lambda x: getattr(x, "name", getattr(x, "__name__", "?")),
)
def test_every_receiver_is_actually_connected(signal, receiver):
    """A module of signal receivers that nothing imports is a module of dead
    code that still passes its own unit tests. ``celery_app`` imports it at the
    bottom of itself for exactly this reason, and this is the assertion that
    notices if that import is ever tidied away."""
    assert receiver in _connected(signal)


def _connected(signal) -> set:
    """The live receivers on *signal*.

    Each entry is ``(lookup_key, receiver)``. Celery's dispatcher holds a weak
    reference by default and the bare function when connected with
    ``weak=False``, so both spellings are unwrapped rather than assumed — and a
    weakref that has expired resolves to ``None``, which is the failure this
    whole test exists to catch.
    """
    live = set()
    for _, reference in signal.receivers:
        resolved = reference() if isinstance(reference, ref) else reference
        if resolved is not None:
            live.add(resolved)
    return live


def _herald_stream(root: logging.Logger) -> StringIO:
    """Configure logging and point Herald's own handler at a buffer."""
    configure_logging(level="INFO")
    stream = StringIO()
    for handler in root.handlers:
        if getattr(handler, "name", None) == "herald":
            handler.setStream(stream)  # type: ignore[attr-defined]
    return stream
