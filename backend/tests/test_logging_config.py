"""The logs a production box actually gets.

Pulse configured no logging at all, which under uvicorn is not the same as
taking Python's defaults. Uvicorn puts handlers on ``uvicorn``, ``uvicorn.error``
and ``uvicorn.access`` and leaves the root logger empty, so every ``app.*``
logger propagated to nothing and fell through to :data:`logging.lastResort` — a
handler pinned at ``WARNING`` with no formatter. Every ``logger.info`` in the
tree was written and dropped, and the warnings that survived arrived as a bare
message with no timestamp, level or source.

The worker did not have the problem, because ``celery --loglevel=info`` hijacks
the root logger on the way up. That is the part worth holding: the same code,
logging to the same names, was kept or dropped depending on which process it ran
in, and the process where it was dropped is the one serving requests.

These tests run against the real uvicorn logging config rather than pytest's,
because pytest attaches its own root handler and would hide the whole bug.
"""
from __future__ import annotations

import logging
import logging.config
from io import StringIO

import pytest
import uvicorn.config

from app.logging_config import (
    LOG_FORMAT,
    configure_logging,
    request_id_var,
    resolve_level,
)


@pytest.fixture
def pristine_logging():
    """Give a test the root logger to itself, and give it back afterwards.

    Restores handlers, level and the levels of everything ``configure_logging``
    touches — pytest's own capture handler lives on the root logger and a test
    that left it off would take the rest of the suite's output with it.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    names = ["httpx", "httpcore", "urllib3", "sqlalchemy.engine", "app.demo"]
    saved_levels = {name: logging.getLogger(name).level for name in names}
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def _capture(root: logging.Logger) -> StringIO:
    """Point Pulse's handler at a buffer instead of stdout."""
    stream = StringIO()
    for handler in root.handlers:
        if getattr(handler, "name", None) == "pulse":
            handler.setStream(stream)
    return stream


# --------------------------------------------------------------------------- #
# The bug                                                                      #
# --------------------------------------------------------------------------- #


def test_uvicorns_own_config_leaves_the_root_logger_empty(pristine_logging):
    """The starting position, stated so the fix below is a fix *of* something.

    Asserted against uvicorn's config rather than against the live root logger,
    which under pytest is already carrying the capture handler that hides the
    whole problem — the bug only shows in a process where nothing else has
    configured logging, which is every process in production.
    """
    configured = set(uvicorn.config.LOGGING_CONFIG["loggers"])

    assert configured == {"uvicorn", "uvicorn.error", "uvicorn.access"}
    assert "root" not in uvicorn.config.LOGGING_CONFIG
    # So `app.*` propagates to an empty root, and Python's fallback takes it
    # from there — at WARNING, which is what dropped every INFO line.
    assert logging.lastResort.level == logging.WARNING
    assert logging.lastResort.formatter is None


def test_an_info_line_survives_once_logging_is_configured(pristine_logging):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("app.demo").info("published content 7 to devto")

    assert "published content 7 to devto" in stream.getvalue()


def test_a_line_carries_its_level_and_source(pristine_logging):
    """A bare message is not a log line. ``lastResort`` has no formatter."""
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("app.demo").warning("broker unavailable, publishing inline")

    written = stream.getvalue()
    assert "WARNING" in written
    assert "app.demo" in written
    # A timestamp, which is what makes two lines an ordering rather than a set.
    assert written.startswith("20")


# --------------------------------------------------------------------------- #
# The request id                                                               #
# --------------------------------------------------------------------------- #


def test_the_request_id_reaches_a_line_written_deep_in_a_service(pristine_logging):
    """The point of the context variable: no plumbing through call signatures."""
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    token = request_id_var.set("abc123def456")
    try:
        logging.getLogger("app.services.publishing_service").info("published")
    finally:
        request_id_var.reset(token)

    assert "[abc123def456]" in stream.getvalue()


def test_a_record_with_no_request_id_still_formats(pristine_logging):
    """``%(request_id)s`` on a library's record must not raise.

    Everything logs through the root handler, including code that has never
    heard of Pulse. A formatter that raised on those would turn an unrelated
    warning into a logging failure inside whatever was serving the request.
    """
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("some.third.party").warning("connection reset")

    assert "[-]" in stream.getvalue()
    assert "connection reset" in stream.getvalue()


def test_an_explicit_request_id_on_a_record_wins(pristine_logging):
    """``extra={"request_id": ...}`` is the caller saying which request it means.

    Overwriting it from the context would be wrong exactly where it is used: a
    task or a retry logging on behalf of the request that *queued* the work,
    rather than whichever one happens to be on this thread now.
    """
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    token = request_id_var.set("thecurrentone")
    try:
        logging.getLogger("app.demo").info(
            "delivering webhook", extra={"request_id": "theoriginalone"}
        )
    finally:
        request_id_var.reset(token)

    assert "[theoriginalone]" in stream.getvalue()
    assert "thecurrentone" not in stream.getvalue()


def test_a_worker_line_is_not_stamped_with_a_stale_request(pristine_logging):
    """A Celery task has no request. ``-`` says so."""
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("app.tasks.publish_tasks").info("publish_due dispatched 3")

    assert "[-]" in stream.getvalue()


# --------------------------------------------------------------------------- #
# Levels                                                                       #
# --------------------------------------------------------------------------- #


def test_a_misspelled_level_falls_back_to_info(pristine_logging):
    """And specifically not to WARNING, which is the bug wearing a new hat.

    ``getattr(logging, name, logging.WARNING)`` would have done that for any
    misspelling — and worse for some, since ``logging.WARN`` and ``logging.FATAL``
    are real attributes and so are ``logging.raiseExceptions`` and friends.
    """
    assert resolve_level("INFO") == logging.INFO
    assert resolve_level("  debug  ") == logging.DEBUG
    assert resolve_level("nonsense") == logging.INFO
    assert resolve_level("") == logging.INFO


def test_the_level_is_honoured(pristine_logging):
    configure_logging(level="WARNING")
    stream = _capture(pristine_logging)

    logging.getLogger("app.demo").info("an ordinary sweep")
    logging.getLogger("app.demo").warning("the broker is gone")

    assert "an ordinary sweep" not in stream.getvalue()
    assert "the broker is gone" in stream.getvalue()


def test_the_chatty_libraries_are_held_back_at_info(pristine_logging):
    """One publish is one line, not a dozen lines of HTTP plumbing."""
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("httpx").info("HTTP Request: POST https://dev.to/api/articles")
    logging.getLogger("app.demo").info("published")

    written = stream.getvalue()
    assert "dev.to/api/articles" not in written
    assert "published" in written


def test_asking_for_debug_gets_debug_everywhere(pristine_logging):
    """A floor that overrode an explicit DEBUG would hide what it was set for."""
    configure_logging(level="DEBUG")
    stream = _capture(pristine_logging)

    logging.getLogger("httpx").debug("connect_tcp.started")

    assert "connect_tcp.started" in stream.getvalue()


# --------------------------------------------------------------------------- #
# Installing it                                                                #
# --------------------------------------------------------------------------- #


def test_configuring_twice_does_not_double_every_line(pristine_logging):
    """``create_app`` is called by every test client, and by uvicorn per worker."""
    configure_logging(level="INFO")
    configure_logging(level="INFO")
    stream = _capture(pristine_logging)

    logging.getLogger("app.demo").info("once")

    assert stream.getvalue().count("once") == 1
    assert len([h for h in pristine_logging.handlers if h.name == "pulse"]) == 1


def test_it_leaves_other_handlers_alone(pristine_logging):
    """pytest's capture handler and uvicorn's live on. Only ours is replaced."""
    someone_else = logging.StreamHandler(StringIO())
    someone_else.name = "not-pulse"
    pristine_logging.addHandler(someone_else)

    configure_logging(level="INFO")

    assert someone_else in pristine_logging.handlers


def test_the_format_names_all_four_things(pristine_logging):
    """Time, level, request, source. Losing any one of them costs a lookup."""
    for field in ("asctime", "levelname", "request_id", "name", "message"):
        # Not `%(field)s` — the level is width-padded so the columns line up.
        assert f"%({field})" in LOG_FORMAT


# --------------------------------------------------------------------------- #
# Through the middleware, and through Celery                                   #
# --------------------------------------------------------------------------- #


def test_a_log_line_written_during_a_request_carries_that_requests_id(
    client, monkeypatch, caplog
):
    """End to end: the id in the header is the id in the log.

    That correspondence is the whole feature. A user quoting the
    ``X-Request-ID`` from a failed call has to be able to find the line, and
    the line is written several layers below anything that knows what a request
    is — here, by the health probe's own logger.
    """
    from app.routers import misc

    monkeypatch.setattr(
        misc, "_check_database", lambda db: misc._Probe(False, "OperationalError")
    )

    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        resp = client.get("/api/v1/health", headers={"X-Request-ID": "deadbeef01"})

    assert resp.status_code == 503
    assert resp.headers["X-Request-ID"] == "deadbeef01"
    failed = [r for r in caplog.records if r.message.startswith("health check failed")]
    assert failed, caplog.records
    assert failed[0].request_id == "deadbeef01"


def test_the_id_does_not_outlive_the_request(client):
    """Reset on the way out, so the next thing logged is not stamped with it."""
    client.get("/api/v1/health", headers={"X-Request-ID": "deadbeef02"})

    assert request_id_var.get() == "-"


def test_a_request_that_explodes_does_not_leak_its_id_into_the_next_one(
    client, monkeypatch
):
    """Hence the ``finally``. A stale id is worse than none.

    The reset has to survive the request unwinding, or the id stays set on a
    worker task that goes on to serve somebody else — and every line it logs is
    then stamped with a request that is long over. An unstamped line sends you
    looking; a wrongly stamped one tells you it already found the answer.
    """
    from app.routers import misc

    def boom(db):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(misc, "_check_database", boom)

    with pytest.raises(RuntimeError, match="probe exploded"):
        client.get("/api/v1/health", headers={"X-Request-ID": "deadbeef03"})

    assert request_id_var.get() == "-"


def test_celery_hands_its_logging_over_to_us():
    """``setup_logging`` has a receiver, which is what disables Celery's own setup.

    Celery's default is ``worker_hijack_root_logger=True``: it clears the root
    logger's handlers and reconfigures it from ``--loglevel``. That is why the
    worker's logs looked complete while the API's did not, and why taking the
    signal over is the only way to make ``LOG_LEVEL`` mean one thing in all
    three units.
    """
    from celery.signals import setup_logging

    from app.tasks import celery_app as module

    assert module.celery_app.conf.worker_hijack_root_logger is True
    assert setup_logging.has_listeners()
    live = [receiver for receiver in setup_logging._live_receivers(None)]
    assert module._configure_logging in live


def test_the_celery_receiver_installs_the_same_handler(pristine_logging):
    from app.tasks.celery_app import _configure_logging

    _configure_logging()

    assert [h.name for h in pristine_logging.handlers if h.name == "pulse"] == [
        "pulse"
    ]
