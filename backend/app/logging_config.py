"""What the application logs, and where it goes.

Nothing configured logging, and under uvicorn that is not the same as "the
defaults apply". Uvicorn installs handlers on its *own* loggers — ``uvicorn``,
``uvicorn.error``, ``uvicorn.access`` — and leaves the root logger alone. Every
``app.*`` logger propagates to that empty root, where Python falls back to
:data:`logging.lastResort`: a bare :class:`~logging.StreamHandler` fixed at
``WARNING`` with no formatter attached.

Two things followed, and both are the kind of thing you only notice when you
need the logs:

* every ``logger.info`` in the tree was dropped. That is the whole operational
  narrative — what each beat sweep dispatched, what was published where, which
  fallback fired, that the pool was disposed on shutdown. The lines were being
  written and thrown away;
* what did survive arrived as the bare message. No timestamp, no level, no
  logger name. ``broker unavailable, publishing inline`` on a line by itself,
  with nothing to say which process wrote it or when.

Journald stamps its own timestamp onto whatever a unit prints, so the second
was survivable. The first was not: the ``WARNING`` floor meant the box logged
only its failures, and a publish that quietly stopped happening looked exactly
like a publish that never had anything to do.

The format is for ``journalctl``, which is how these logs are actually read
(see ``deploy/DEPLOYMENT.md``) — plain text a person can skim, not JSON for a
shipper nobody has installed.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from app.config import settings

#: The request being served on this task, for correlating log lines with the
#: ``X-Request-ID`` the caller was handed back. A context variable rather than
#: anything threaded through call signatures: the services that do the logging
#: are the ones with no idea a request exists, which is exactly why they had
#: nothing to log it against. Workers leave it at the default — a Celery task
#: has no request, and ``-`` says so rather than inheriting a stale id.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

#: Level, request id, source, message. The request id sits early because
#: grepping one out of a day of logs is the thing it exists for.
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"

#: Third parties that log a line per operation at INFO. Raising the app to INFO
#: without these turns one publish into a dozen lines of HTTP plumbing, which
#: is how a log stops being read.
_NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    # Not the echo flag — that is off — but the pool's own chatter.
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "multipart": logging.WARNING,
}

#: Marks the handler as ours, so :func:`configure_logging` can recognise its own
#: work and replace it rather than stacking a second copy of every line.
_HANDLER_NAME = "pulse"


class RequestIDFilter(logging.Filter):
    """Put the current request id on every record.

    A filter rather than a formatter override because the id has to exist on
    records this application never created — uvicorn's, Celery's, a library's.
    A ``%(request_id)s`` in the format string raises for any record without the
    attribute, and a logging call that raises inside a request handler is a
    worse outage than the one you were trying to diagnose.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp *record* with the current request id, and keep it.

        Never drops a record — the return is always ``True``. A record that
        already carries a ``request_id`` keeps it, which is what lets a task
        set its own without this overwriting it.
        """
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


def resolve_level(name: str) -> int:
    """A level name to its number, falling back to INFO.

    ``LOG_LEVEL`` is a free-text environment variable, and a typo in it should
    not stop the process from starting — nor silently mean ``WARNING``, which
    is what ``getattr(logging, name, ...)`` on a misspelling would produce via
    the module's other integer attributes.
    """
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    return level if isinstance(level, int) else logging.INFO


def configure_logging(*, level: str | None = None) -> None:
    """Install Pulse's handler on the root logger. Idempotent.

    Called from :func:`app.main.create_app` and from the Celery ``setup_logging``
    signal, so every entry point that runs application code gets the same
    output: the API under uvicorn, the worker, beat, and ``python -m app.seed``.

    Only Pulse's own handler is replaced. Uvicorn's handlers stay on uvicorn's
    loggers, which is what keeps access logs looking like access logs; pytest's
    capture handlers are attached elsewhere too and are likewise untouched.
    """
    resolved = resolve_level(level or settings.log_level)
    root = logging.getLogger()

    for existing in [h for h in root.handlers if h.name == _HANDLER_NAME]:
        root.removeHandler(existing)

    # stdout, not stderr: these are ordinary operational lines, and a unit whose
    # every log line is stderr makes `systemctl status` look like a fire.
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIDFilter())
    root.addHandler(handler)
    root.setLevel(resolved)

    # ``LOG_LEVEL=DEBUG`` is nobody's steady state — it is set by somebody
    # chasing a specific thing, and half of what they are chasing is usually in
    # the HTTP layer. So the floors apply at every ordinary level and lift
    # entirely for DEBUG, rather than being blended with it: a floor that still
    # applied would hide exactly the lines the flag was turned on for.
    everything = resolved <= logging.DEBUG
    for name, floor in _NOISY.items():
        logging.getLogger(name).setLevel(resolved if everything else floor)
