"""Whether a failure is worth trying again, in one place.

Herald already knows the answer per domain, and each domain says it in its own
vocabulary: :class:`app.services.llm_router.LLMError` carries a ``retryable``
flag, :mod:`app.services.publishers.base` splits the same question into three
exception classes, ``github_client`` into two more, and the task layer restates
it a fourth way as ``autoretry_for=(OperationalError, ConnectionError, OSError)``.

Every one of those is right where it is used, and none of them is available to
the code that has to *report* a failure. A worker logging a task that died gets
an exception and nothing else, so the line it writes says only that something
went wrong — which is the line an operator reads at 3am and cannot act on. "The
platform rate-limited us and the row is parked until it lifts" and "the stored
token was rejected and no amount of waiting fixes it" are the same log line
today, and they call for opposite responses.

So the vocabulary is unified here, for logs and diagnostics only. **This module
decides nothing.** It does not drive a retry, park a row, or open a breaker —
those decisions stay with the code that owns the outcome, exactly where they
are now, because each of them knows things this cannot (whether a POST is
idempotent, what the platform's ``Retry-After`` said, how many attempts the row
has already spent). Reclassification here would be a second opinion overriding
a first-hand one.

Three answers rather than two. :attr:`ErrorClass.UNKNOWN` is not a hedge — it
is the honest report for an exception nobody anticipated, and it is what makes
the classifier safe to leave running: a wrong ``PERMANENT`` on something novel
would tell an operator to stop looking.
"""
from __future__ import annotations

from enum import StrEnum

import httpx
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.orm.exc import StaleDataError

from app.services.github_client import GitHubNotFound, GitHubRateLimited
from app.services.publishers.base import CredentialError, PublishError, RateLimited

#: HTTP statuses that mean "not now" rather than "not ever". 429 is the explicit
#: one; the 5xx family is the server saying the failure was on its side, which
#: it may not be next time. Deliberately *not* the same set as
#: ``publishers.base._TRANSIENT_STATUSES``: that one decides whether to replay a
#: request and has to worry about whether a POST already landed, which is a
#: harder question than the one asked here.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


class ErrorClass(StrEnum):
    """What an operator should do about a failure.

    A ``StrEnum`` so it formats as its own value in a log line and serialises
    without a ``.value`` at every call site.
    """

    #: Came from outside and may not recur: a timeout, a rate limit, a 5xx, a
    #: database connection that dropped. Worth another attempt, on whatever
    #: schedule the owning code has already decided.
    RETRYABLE = "retryable"

    #: Replaying this spends the budget twice for the same answer: a rejected
    #: credential, content the platform refused, a bug in the arguments.
    #: Something has to change before it can succeed.
    PERMANENT = "permanent"

    #: Not anticipated. Says so rather than guessing, because a guess here is
    #: read as a fact.
    UNKNOWN = "unknown"


#: Exception types whose class alone settles it, most specific first — the
#: ordering matters because several of these are subclasses of others.
#:
#: A tuple of pairs rather than a dict: lookup is by ``isinstance`` (a subclass
#: of ``CredentialError`` is still a credential problem), and a dict keyed by
#: type would only match the exact class.
_BY_TYPE: tuple[tuple[type[BaseException], ErrorClass], ...] = (
    # --- Herald's own vocabulary -------------------------------------------
    # The two specific ones come before their shared base, which is the arm
    # that would otherwise answer for them.
    (CredentialError, ErrorClass.PERMANENT),
    (RateLimited, ErrorClass.RETRYABLE),
    # "Anything else that went wrong while publishing" — documented as possibly
    # transient and retried with backoff by the code that raises it, so that is
    # what it is reported as.
    (PublishError, ErrorClass.RETRYABLE),
    (GitHubRateLimited, ErrorClass.RETRYABLE),
    (GitHubNotFound, ErrorClass.PERMANENT),
    # A bare ``GitHubError`` is deliberately absent: the same class is raised
    # for a transport failure and for a payload that was not the expected
    # shape, and those are opposite answers. UNKNOWN is the true one.
    # --- The clock ---------------------------------------------------------
    # A sweep that ran out of its budget did not fail at what it was doing; it
    # ran out of time to finish. The next tick picks up the same watermark, so
    # this is the definition of worth trying again.
    (SoftTimeLimitExceeded, ErrorClass.RETRYABLE),
    (TimeLimitExceeded, ErrorClass.RETRYABLE),
    # --- Transport ---------------------------------------------------------
    (httpx.TimeoutException, ErrorClass.RETRYABLE),
    (httpx.TransportError, ErrorClass.RETRYABLE),
    # --- The database ------------------------------------------------------
    # Somebody else wrote the row between the read and the commit. The work is
    # still valid on a reload, which is what the 409 tells an API caller to do
    # and what a task's next attempt does by itself.
    (StaleDataError, ErrorClass.RETRYABLE),
    # These three are the statement being wrong — a constraint, a type, a bad
    # query. Identical on every replay.
    (IntegrityError, ErrorClass.PERMANENT),
    (ProgrammingError, ErrorClass.PERMANENT),
    (DataError, ErrorClass.PERMANENT),
    # Below the three above, which are all subclasses of DatabaseError but not
    # of these: a dropped connection or an exhausted pool, which is what the
    # task layer already retries on.
    (OperationalError, ErrorClass.RETRYABLE),
    (InterfaceError, ErrorClass.RETRYABLE),
    # --- The standard library ----------------------------------------------
    # ConnectionError and TimeoutError are OSError subclasses; listing them is
    # documentation rather than dispatch.
    (ConnectionError, ErrorClass.RETRYABLE),
    (TimeoutError, ErrorClass.RETRYABLE),
    (OSError, ErrorClass.RETRYABLE),
    # A bug, or content that is the wrong shape. The next attempt reads the
    # same rows and makes the same mistake.
    (TypeError, ErrorClass.PERMANENT),
    (ValueError, ErrorClass.PERMANENT),
    (LookupError, ErrorClass.PERMANENT),
    (AttributeError, ErrorClass.PERMANENT),
    (ZeroDivisionError, ErrorClass.PERMANENT),
)


def classify(exc: BaseException) -> ErrorClass:
    """What kind of failure *exc* is, for reporting.

    Checked in three passes, narrowest first:

    1. an explicit ``retryable`` flag, because a type that answers the question
       itself is more specific than anything inferred from its class —
       :class:`~app.services.llm_router.LLMRateLimited` and
       :class:`~app.services.llm_router.LLMModelUnavailable` are both
       ``LLMError`` and disagree;
    2. :data:`_BY_TYPE`, in order;
    3. an HTTP status, for the one type where the class says nothing and the
       response says everything.

    Never raises. A classifier called from an exception handler that can itself
    throw turns a logged failure into a lost one.
    """
    # An httpx.HTTPStatusError is a TransportError-adjacent type that carries
    # the answer on its response, so it is settled before the type table gets
    # to generalise about it.
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status(exc.response.status_code)

    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        return ErrorClass.RETRYABLE if retryable else ErrorClass.PERMANENT

    for kind, verdict in _BY_TYPE:
        if isinstance(exc, kind):
            return verdict

    return ErrorClass.UNKNOWN


def _from_status(status: int) -> ErrorClass:
    """A response status to a verdict.

    Anything outside the retryable set that is still a failure is permanent —
    a 401, a 404, a 422 — and a 2xx/3xx reaching here means somebody raised for
    a status that did not fail, which is not something to guess about.
    """
    if status in _RETRYABLE_STATUSES:
        return ErrorClass.RETRYABLE
    if 400 <= status < 500:
        return ErrorClass.PERMANENT
    return ErrorClass.UNKNOWN


def describe(exc: BaseException) -> str:
    """One field for a log line: ``class=permanent type=CredentialError``.

    The type name goes with the verdict deliberately. The verdict is what an
    operator decides on and the type is what they grep for, and a line carrying
    only the first is a line that has to be joined against the traceback above
    it to be useful.
    """
    return f"class={classify(exc)} type={type(exc).__name__}"
