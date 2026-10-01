"""In-adapter retries, Retry-After parsing, and what must never be replayed.

The rule this file exists to pin down: a retry is only allowed when replaying
the call cannot change the outcome. A GET is free. A POST is not — a duplicate
article is a worse failure than a missing one — so it is replayed only when the
platform said it did not process the request (429/503) or when the request
provably never left the building (a connect error).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.models.publication import Platform
from app.services.publishers import base
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    PublishError,
    PublishRequest,
    PublishResult,
    RateLimited,
)

_URL = "https://example.test/api/articles"


class _Probe(Adapter):
    """The smallest adapter that can make a request."""

    platform = Platform.DEVTO
    display_name = "Probe"
    implemented = True

    def publish(  # pragma: no cover
        self, request: PublishRequest, credentials: dict
    ) -> PublishResult:
        raise NotImplementedError


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Record what the retry loop would have slept for, without sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(base, "_sleep", slept.append)
    return slept


def _response(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        json={"detail": "nope"} if status >= 400 else {"ok": True},
        request=httpx.Request("GET", _URL),
    )


@pytest.fixture
def transport(monkeypatch):
    """Queue up responses/exceptions; returns the list of calls made."""
    calls: list[str] = []

    def install(*outcomes):
        queued = list(outcomes)

        def fake_request(method, url, **kwargs):
            calls.append(method)
            outcome = queued.pop(0) if queued else queued
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(base.httpx, "request", fake_request)
        return calls

    return install


# --------------------------------------------------------------------------- #
# Retry-After parsing                                                         #
# --------------------------------------------------------------------------- #


def test_retry_after_reads_a_delay_in_seconds():
    assert base._retry_after(_response(429, headers={"Retry-After": "42"})) == 42.0


def test_retry_after_reads_an_http_date():
    later = datetime.now(UTC) + timedelta(seconds=120)
    seconds = base._retry_after(
        _response(429, headers={"Retry-After": format_datetime(later)})
    )
    assert seconds is not None
    # Allow for the clock moving between building the header and parsing it.
    assert 110 <= seconds <= 121


def test_retry_after_in_the_past_is_zero_not_negative():
    earlier = datetime.now(UTC) - timedelta(hours=1)
    assert base._retry_after(
        _response(429, headers={"Retry-After": format_datetime(earlier)})
    ) == 0.0


@pytest.mark.parametrize("raw", ["", "soon", "next tuesday"])
def test_unusable_retry_after_reads_as_no_guidance(raw):
    assert base._retry_after(_response(429, headers={"Retry-After": raw})) is None


def test_missing_retry_after_reads_as_no_guidance():
    assert base._retry_after(_response(429)) is None


# --------------------------------------------------------------------------- #
# What gets retried                                                           #
# --------------------------------------------------------------------------- #


def test_get_retries_a_503_and_returns_the_eventual_success(probe, transport):
    calls = transport(_response(503), _response(503), _response(200))
    resp = probe._request("GET", _URL)

    assert resp.status_code == 200
    assert len(calls) == 3


def test_get_gives_up_once_the_budget_is_spent(probe, transport):
    calls = transport(_response(500), _response(500), _response(500), _response(200))

    with pytest.raises(PublishError, match="returned 500"):
        probe._request("GET", _URL)
    # Default budget is 2 retries: three calls total, and the fourth response
    # (the success) is never reached.
    assert len(calls) == 3


def test_post_is_not_replayed_after_a_500(probe, transport):
    """The write may already have landed. A second POST would double-post."""
    calls = transport(_response(500), _response(200))

    with pytest.raises(PublishError, match="returned 500"):
        probe._request("POST", _URL)
    assert len(calls) == 1


def test_post_is_replayed_after_a_429(probe, transport):
    """A rate limit says the platform did not process the request."""
    calls = transport(_response(429, headers={"Retry-After": "1"}), _response(200))
    resp = probe._request("POST", _URL)

    assert resp.status_code == 200
    assert len(calls) == 2


def test_post_is_replayed_after_a_503(probe, transport):
    calls = transport(_response(503), _response(200))
    assert probe._request("POST", _URL).status_code == 200
    assert len(calls) == 2


def test_post_is_replayed_when_the_request_never_got_out(probe, transport):
    calls = transport(
        httpx.ConnectError("no route to host", request=httpx.Request("POST", _URL)),
        _response(200),
    )
    assert probe._request("POST", _URL).status_code == 200
    assert len(calls) == 2


def test_post_is_not_replayed_after_a_read_timeout(probe, transport):
    """The request was sent. Nobody knows whether it landed."""
    calls = transport(
        httpx.ReadTimeout("timed out", request=httpx.Request("POST", _URL)),
        _response(200),
    )
    with pytest.raises(PublishError, match="request failed"):
        probe._request("POST", _URL)
    assert len(calls) == 1


def test_get_is_replayed_after_a_read_timeout(probe, transport):
    calls = transport(
        httpx.ReadTimeout("timed out", request=httpx.Request("GET", _URL)),
        _response(200),
    )
    assert probe._request("GET", _URL).status_code == 200
    assert len(calls) == 2


def test_credential_failures_are_never_replayed(probe, transport):
    calls = transport(_response(401), _response(200))

    with pytest.raises(CredentialError):
        probe._request("GET", _URL)
    assert len(calls) == 1


def test_a_4xx_that_is_not_transient_is_not_replayed(probe, transport):
    calls = transport(_response(422), _response(200))

    with pytest.raises(PublishError, match="returned 422"):
        probe._request("GET", _URL)
    assert len(calls) == 1


def test_retries_can_be_switched_off_per_call(probe, transport):
    calls = transport(_response(503), _response(200))

    with pytest.raises(PublishError):
        probe._request("GET", _URL, retries=0)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Rate limiting                                                               #
# --------------------------------------------------------------------------- #


def test_a_429_surfaces_as_rate_limited_carrying_the_wait(probe, transport):
    transport(*[_response(429, headers={"Retry-After": "90"})] * 3)

    with pytest.raises(RateLimited) as caught:
        probe._request("GET", _URL)
    assert caught.value.retry_after == 90.0


def test_the_platforms_own_wait_beats_our_backoff(probe, transport, no_waiting):
    transport(_response(429, headers={"Retry-After": "7"}), _response(200))
    probe._request("GET", _URL)

    assert no_waiting == [7.0]


def test_a_wait_longer_than_the_ceiling_is_handed_upwards_not_slept_through(
    probe, transport, no_waiting
):
    """A long ``Retry-After`` ends this loop rather than being clamped into it.

    Clamping is the failure worth naming: it slept thirty seconds against a
    platform that said "come back in a day", retried anyway, and burned the
    publication's whole retry budget doing it — so the row arrived at ``_defer``
    with nothing left and failed outright. Raising instead lets
    ``publishing_service`` park it until the platform's own time.
    """
    calls = transport(_response(429, headers={"Retry-After": "99999"}), _response(200))

    with pytest.raises(RateLimited) as caught:
        probe._request("GET", _URL)

    assert caught.value.retry_after == 99999.0
    assert len(calls) == 1
    assert no_waiting == []


def test_a_wait_inside_the_ceiling_is_still_honoured_in_process(
    probe, transport, no_waiting
):
    """The other side of the line: a short wait is exactly what this loop is for."""
    from app.config import settings

    wait = settings.publish_retry_max_backoff_seconds
    calls = transport(
        _response(429, headers={"Retry-After": str(wait)}), _response(200)
    )
    assert probe._request("GET", _URL).status_code == 200

    assert len(calls) == 2
    assert no_waiting == [wait]


# --------------------------------------------------------------------------- #
# A 503 that says when                                                        #
# --------------------------------------------------------------------------- #
#
# ``Retry-After`` is not a 429 header (RFC 9110 §10.2.3) — a 503 carries it to
# announce a maintenance window, and it answers the same question. Pulse read
# it only on 429, so a platform saying "back in an hour" got the one-second
# backoff, the whole in-process budget inside the first blink of the outage, and
# then a row parked on ``retry_defer_seconds`` as though nothing had been said.


def test_a_503_that_says_when_is_a_rate_limit_carrying_the_wait(probe):
    error = probe._translate(_response(503, headers={"Retry-After": "900"}))

    assert isinstance(error, RateLimited)
    assert error.retry_after == 900.0


def test_a_503_can_say_when_with_a_date_too(probe):
    later = datetime.now(UTC) + timedelta(seconds=600)
    error = probe._translate(
        _response(503, headers={"Retry-After": format_datetime(later)})
    )

    assert isinstance(error, RateLimited)
    assert error.retry_after is not None
    assert 590 <= error.retry_after <= 601


def test_a_bare_503_stays_a_plain_error_on_our_own_schedule(probe):
    """The other side of the branch, and the one that must not move.

    With no header there is nothing to honour, so a 503 keeps exactly the
    behaviour it has always had — retryable, and replayable even for a POST,
    because 503 still means "I did not process this".
    """
    error = probe._translate(_response(503))

    assert type(error) is PublishError
    assert "returned 503" in str(error)


@pytest.mark.parametrize("raw", ["", "soon", "when we're back"])
def test_a_503_whose_header_is_unusable_falls_back_to_our_own_schedule(probe, raw):
    error = probe._translate(_response(503, headers={"Retry-After": raw}))

    assert type(error) is PublishError


def test_a_503_is_still_replayed_for_a_post(probe, transport, no_waiting):
    """Reclassifying the error must not cost 503 its POST replay.

    ``_is_retryable`` reaches that decision through the *response* status, not
    the error type, so a 503 stays in :data:`_REJECTED_WITHOUT_PROCESSING`
    whichever branch of ``_translate`` built the error.
    """
    calls = transport(
        _response(503, headers={"Retry-After": "2"}), _response(200)
    )
    assert probe._request("POST", _URL).status_code == 200

    assert calls == ["POST", "POST"]
    assert no_waiting == [2.0]


def test_the_wait_from_a_503_beats_our_backoff(probe, transport, no_waiting):
    calls = transport(_response(503, headers={"Retry-After": "12"}), _response(200))
    probe._request("GET", _URL)

    assert len(calls) == 2
    # Not the ~1s the exponential window would have chosen on a first attempt.
    assert no_waiting == [12.0]


def test_a_long_503_wait_is_handed_upwards_not_slept_through(
    probe, transport, no_waiting
):
    """The case the fix is really for: an announced outage longer than the loop.

    Sleeping the ceiling and retrying anyway comes back before the platform said
    to *and* spends the publication's budget doing it, so the row reaches
    ``publishing_service._defer`` with nothing left. Raising parks it until the
    platform's own time instead.
    """
    calls = transport(_response(503, headers={"Retry-After": "3600"}), _response(200))

    with pytest.raises(RateLimited) as caught:
        probe._request("POST", _URL)

    assert caught.value.retry_after == 3600.0
    assert len(calls) == 1
    assert no_waiting == []


def test_backoff_grows_and_stays_inside_its_window(probe, transport, no_waiting):
    transport(_response(503), _response(503), _response(200))
    probe._request("GET", _URL)

    from app.config import settings

    base_window = settings.publish_retry_backoff_seconds
    first, second = no_waiting
    # Full jitter: each wait lands in the top half of its own window, and the
    # window doubles per attempt.
    assert base_window / 2 <= first <= base_window
    assert base_window <= second <= base_window * 2
