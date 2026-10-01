"""The classifier is read by a person deciding what to do next.

:mod:`app.services.error_class` exists because the answer to "is this worth
retrying" was spread across four vocabularies — an exception flag in the LLM
router, a class hierarchy in the publishing adapters, two more classes in the
GitHub client, and a tuple of types in each task decorator — and none of them
was reachable from the code that writes the log line. So the classification is
only ever *reported*, and that is precisely what makes it dangerous to get
wrong: nothing downstream will contradict it. A ``permanent`` on something
transient reads as "stop looking", and that is a decision made by a log line.

Two properties are asserted throughout. The verdict for each family is what the
code that raises it already believes, and the classifier cannot raise — it runs
inside exception handlers and signal receivers, where an exception of its own
turns a failure that was being logged into one that is lost.
"""
from __future__ import annotations

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.orm.exc import StaleDataError

from app.services.error_class import ErrorClass, classify, describe
from app.services.github_client import GitHubError, GitHubNotFound, GitHubRateLimited
from app.services.llm_router import LLMError, LLMModelUnavailable, LLMRateLimited
from app.services.publishers.base import CredentialError, PublishError, RateLimited


def _db_error(kind: type[Exception]) -> Exception:
    """A SQLAlchemy DBAPI error, which needs three positional arguments."""
    return kind("SELECT 1", {}, Exception("boom"))


RETRYABLE = [
    # The platform said "later", in each of the three dialects Pulse speaks.
    RateLimited("slow down", retry_after=30.0),
    GitHubRateLimited("rate limited"),
    LLMRateLimited("quota", retry_after=60.0),
    # Anything else that went wrong mid-publish. The publisher retries these
    # with backoff, so reporting them as permanent would contradict the code.
    PublishError("the platform hiccupped"),
    # Ran out of clock rather than failed. The next tick resumes the watermark.
    SoftTimeLimitExceeded(),
    TimeLimitExceeded(),
    # Transport.
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
    httpx.RemoteProtocolError("truncated"),
    ConnectionResetError("peer went away"),
    TimeoutError("no answer"),
    OSError("network unreachable"),
    # The database dropped the connection, or the pool did.
    _db_error(OperationalError),
    _db_error(InterfaceError),
    # Somebody else committed the row first; a reload makes the work valid again.
    StaleDataError("version mismatch"),
]

PERMANENT = [
    # No amount of waiting makes a rejected token acceptable.
    CredentialError("token rejected"),
    GitHubNotFound("no such repo"),
    LLMModelUnavailable("model retired"),
    # The statement itself is wrong, and will be next time too.
    _db_error(IntegrityError),
    _db_error(ProgrammingError),
    _db_error(DataError),
    # A bug, or content the wrong shape. The replay reads the same rows.
    TypeError("NoneType is not subscriptable"),
    ValueError("not an int"),
    KeyError("platform"),
    IndexError("list index out of range"),
    AttributeError("no attribute 'slug'"),
    ZeroDivisionError("division by zero"),
]


@pytest.mark.parametrize("exc", RETRYABLE, ids=lambda e: type(e).__name__)
def test_a_failure_that_may_pass_is_reported_as_retryable(exc):
    assert classify(exc) is ErrorClass.RETRYABLE


@pytest.mark.parametrize("exc", PERMANENT, ids=lambda e: type(e).__name__)
def test_a_failure_that_will_recur_is_reported_as_permanent(exc):
    assert classify(exc) is ErrorClass.PERMANENT


def test_a_credential_failure_outranks_the_publish_error_it_inherits_from():
    """Ordering, not coincidence.

    ``CredentialError`` is a ``PublishError`` and the table matches by
    ``isinstance``, so whichever entry comes first wins. Getting this backwards
    would report every rejected token as worth retrying — the exact failure the
    split exists to distinguish, and the one where a wrong answer costs a user
    four attempts and a delay before anyone is told to reconnect.
    """
    assert classify(CredentialError("bad token")) is ErrorClass.PERMANENT
    assert classify(PublishError("something else")) is ErrorClass.RETRYABLE
    # And the rate limit likewise, which is the other subclass of the same base.
    assert classify(RateLimited("later")) is ErrorClass.RETRYABLE


def test_the_type_s_own_answer_beats_anything_inferred_from_its_class():
    """``LLMError`` carries the verdict as a flag, and its subclasses disagree.

    Both of these are the same class to an ``isinstance`` table. Only the flag
    separates "the provider is up and said come back" from "this model does not
    exist any more", which is the difference between waiting and editing config.
    """
    assert classify(LLMError("429 from the provider", retryable=True)) is (
        ErrorClass.RETRYABLE
    )
    assert classify(LLMError("malformed request")) is ErrorClass.PERMANENT


def test_an_exception_nobody_anticipated_says_so():
    """UNKNOWN is a real answer, not a fallback to the safe-looking one.

    A novel exception classified as ``permanent`` tells an operator to stop
    retrying something that might well recover; as ``retryable`` it tells them
    to wait out something that never will. Neither is knowable, so neither is
    claimed.
    """

    class SomethingNew(Exception):
        pass

    assert classify(SomethingNew("first time for everything")) is ErrorClass.UNKNOWN


def test_a_bare_github_error_is_not_guessed_at():
    """The same class is raised for a transport failure and a bad payload."""
    assert classify(GitHubError("GitHub request to /repos failed")) is (
        ErrorClass.UNKNOWN
    )


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 507, 509])
def test_a_response_status_that_means_not_now_is_retryable(status):
    assert classify(_status_error(status)) is ErrorClass.RETRYABLE


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422])
def test_a_response_status_that_means_not_ever_is_permanent(status):
    assert classify(_status_error(status)) is ErrorClass.PERMANENT


def test_a_status_error_is_read_off_the_response_not_off_its_base_class():
    """``HTTPStatusError`` is an ``httpx.HTTPError`` sitting next to the
    transport types, and the transport arm calls every one of those retryable.
    A 401 from a platform is the single most common permanent failure Pulse
    has, so being generalised into "try again" would misreport the majority of
    real credential problems."""
    assert isinstance(_status_error(401), httpx.HTTPError)
    assert classify(_status_error(401)) is ErrorClass.PERMANENT


def test_a_status_nobody_should_have_raised_for_is_not_classified():
    """A 2xx reaching the classifier means somebody raised for a success."""
    assert classify(_status_error(204)) is ErrorClass.UNKNOWN


def test_a_lying_retryable_attribute_is_ignored_rather_than_believed():
    """Only a real boolean answers the question.

    The flag is duck-typed on purpose — it is what makes ``LLMError`` work
    without this module importing it — and duck typing means anything at all can
    turn up carrying the name. A truthy string is not a verdict.
    """

    class Impostor(ValueError):
        retryable = "yes, very"

    # Falls through to the type table, which knows what a ValueError is.
    assert classify(Impostor("...")) is ErrorClass.PERMANENT


def test_classifying_something_that_is_not_an_exception_does_not_raise():
    """Called from handlers that were handed whatever Celery had."""
    assert classify(BaseException("bare")) is ErrorClass.UNKNOWN
    assert classify(KeyboardInterrupt()) is ErrorClass.UNKNOWN


def test_the_description_carries_the_verdict_and_the_type():
    """One field, both halves — see :func:`app.services.error_class.describe`."""
    assert describe(CredentialError("nope")) == "class=permanent type=CredentialError"
    assert describe(httpx.ConnectTimeout("x")) == "class=retryable type=ConnectTimeout"


def test_the_verdict_formats_as_its_own_value_in_a_log_line():
    """A ``StrEnum``, so no call site has to remember ``.value``."""
    assert f"{ErrorClass.RETRYABLE}" == "retryable"
    assert f"{classify(ValueError('x'))}" == "permanent"


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/posts")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )
