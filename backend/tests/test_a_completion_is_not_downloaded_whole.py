"""How much of a provider's answer Herald is willing to hold in memory.

Every request out of :mod:`app.services.llm_router` bounds itself on the way
*out* — ``max_tokens``, and the largest budget anything here asks for is a
long-form article's ~8,000. Nothing bounded the way in. ``httpx.post`` returns
only once the whole body is buffered, so the size of the reply was decided
entirely by the far end and Herald's first opportunity to have an opinion about
it was after it had already paid: ``resp.json()`` on whatever arrived, and the
``_short()`` clip that trims a body for a log line runs on a string that is
already in memory.

That matters here more than it looks. The base URLs are settings
(``OPENROUTER_BASE_URL`` and its three siblings), the completion path is what
every Celery generation task ends up in, and production runs two workers on a
box with 4 GB shared with five other applications. A provider having a bad day —
a proxy answering a completion with an HTML error page, a compat layer that
loops — is not something Herald can fix from here. What it can do is stop
reading.

Same rule and same mechanism as :data:`app.services.feeds.MAX_FEED_BYTES` and
the streamed reads in :mod:`app.services.link_check`, which is the point: an
outbound fetch whose response size somebody else chooses gets read in chunks
against a cap, because the only place a body can be refused cheaply is before it
is in memory.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services import llm_router


def _provider() -> llm_router.Provider:
    return llm_router.Provider(
        name="openrouter",
        api_key="key",
        base_url="https://openrouter.ai/api/v1",
        model="primary/model",
    )


def _completion(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    ).encode()


def _serving(body: bytes, *, status_code: int = 200, headers: dict | None = None):
    """A router whose ``_post`` goes out over a mock transport.

    Deliberately not a stub of ``_post`` itself: the cap lives *inside* it, so a
    test that replaced it would assert on nothing. Only the socket is faked.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, headers=headers or {})

    return httpx.MockTransport(handler)


@pytest.fixture
def over_the_wire(monkeypatch):
    """Point ``_post``'s client at a transport instead of a socket."""
    holder: dict = {}
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = holder["transport"]
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_router.httpx, "Client", client)
    return holder


def test_an_ordinary_completion_still_comes_back(over_the_wire):
    """The guard on everything below: the streamed read is still a read."""
    over_the_wire["transport"] = _serving(_completion("A post about shipping."))

    # ``.text`` because ``_call`` returns a ``_Served`` — the text plus the
    # provider's raw ``usage`` block, which this module has no assertions about.
    text = llm_router._call(
        _provider(),
        [{"role": "user", "content": "hi"}],
        model="primary/model",
        temperature=0.7,
        max_tokens=100,
        timeout=5.0,
    ).text

    assert text == "A post about shipping."


def test_a_completion_larger_than_the_cap_is_refused(over_the_wire):
    """And refused *as an ``LLMError``*, so the chain treats it as this
    provider failing rather than as something to crash the generation with."""
    over_the_wire["transport"] = _serving(
        b'{"choices": [' + b"x" * (llm_router.MAX_RESPONSE_BYTES + 1) + b"]}"
    )

    with pytest.raises(llm_router.LLMError) as caught:
        llm_router._call(
            _provider(),
            [{"role": "user", "content": "hi"}],
            model="primary/model",
            temperature=0.7,
            max_tokens=100,
            timeout=5.0,
        )

    assert "openrouter" in str(caught.value), "the log line has to name a provider"
    assert "4 MB" in str(caught.value)


def test_an_oversized_answer_is_not_retried_on_the_same_provider(over_the_wire):
    """Asking the same endpoint the same question buys another few megabytes.

    ``retryable`` is what the in-process second sweep reads, and it is the one
    thing that must not be true here — a rate limit is worth waiting out, a
    provider spraying the socket is not.
    """
    over_the_wire["transport"] = _serving(b"y" * (llm_router.MAX_RESPONSE_BYTES + 1))

    with pytest.raises(llm_router.LLMError) as caught:
        llm_router._call(
            _provider(),
            [{"role": "user", "content": "hi"}],
            model="primary/model",
            temperature=0.7,
            max_tokens=100,
            timeout=5.0,
        )

    assert caught.value.retryable is False


def test_the_body_is_abandoned_rather_than_read_to_the_end(over_the_wire):
    """The whole point: the cap has to fire while the download is still running.

    A check on a body that has already been buffered is a check that has already
    lost. The transport hands back a lazily generated stream and counts what is
    pulled from it — if the reply is read to the end, the counter says so and
    the cap was decorative.
    """
    chunk = b"z" * (256 * 1024)
    pulled = {"bytes": 0}

    def stream():
        # Far more than the cap, one chunk at a time, so "stopped early" is
        # measurable rather than inferred.
        for _ in range(4 * llm_router.MAX_RESPONSE_BYTES // len(chunk)):
            pulled["bytes"] += len(chunk)
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())

    over_the_wire["transport"] = httpx.MockTransport(handler)

    with pytest.raises(llm_router.LLMError):
        llm_router._call(
            _provider(),
            [{"role": "user", "content": "hi"}],
            model="primary/model",
            temperature=0.7,
            max_tokens=100,
            timeout=5.0,
        )

    assert pulled["bytes"] <= llm_router.MAX_RESPONSE_BYTES + len(chunk), (
        f"read {pulled['bytes']} bytes off a body it had already refused — the "
        "cap has to stop the download, not describe it afterwards"
    )


def test_a_body_just_under_the_cap_is_still_served(over_the_wire):
    """The boundary from the other side, so the cap cannot be off by a chunk."""
    filler = "w" * (llm_router.MAX_RESPONSE_BYTES - 200)
    over_the_wire["transport"] = _serving(_completion(filler))

    text = llm_router._call(
        _provider(),
        [{"role": "user", "content": "hi"}],
        model="primary/model",
        temperature=0.7,
        max_tokens=100,
        timeout=5.0,
    ).text

    assert text == filler


def test_an_error_body_is_still_read_and_quoted(over_the_wire):
    """A refusal has to survive the streaming rewrite.

    ``_status_error`` reads the status line, the body and ``Retry-After`` off
    what ``_post`` returns; that used to be an ``httpx.Response`` and is now a
    small stand-in, so this is what says the stand-in still carries all three.
    """
    over_the_wire["transport"] = _serving(
        b'{"error": {"message": "rate limit exceeded"}}',
        status_code=429,
        headers={"Retry-After": "42"},
    )

    with pytest.raises(llm_router.LLMRateLimited) as caught:
        llm_router._call(
            _provider(),
            [{"role": "user", "content": "hi"}],
            model="primary/model",
            temperature=0.7,
            max_tokens=100,
            timeout=5.0,
        )

    assert caught.value.retry_after == 42
    assert "rate limit exceeded" in str(caught.value)


def test_a_body_that_is_not_utf8_is_reported_rather_than_raised(over_the_wire):
    """A provider sending bytes that will not decode is still evidence.

    Decoding with ``replace`` rather than strict, because the alternative is a
    ``UnicodeDecodeError`` escaping the router as something no caller catches —
    and losing the one thing that would have said what the provider sent.
    """
    over_the_wire["transport"] = _serving(b"\xff\xfe not json at all", status_code=500)

    with pytest.raises(llm_router.LLMError) as caught:
        llm_router._call(
            _provider(),
            [{"role": "user", "content": "hi"}],
            model="primary/model",
            temperature=0.7,
            max_tokens=100,
            timeout=5.0,
        )

    assert "500" in str(caught.value)


def test_the_cap_is_far_above_the_largest_completion_anything_asks_for(over_the_wire):
    """A backstop, not a limit generation can run into.

    If the cap ever drifted down near a real answer, the symptom would be
    articles failing to generate with an error about response size — which reads
    as a provider fault and is not one. The largest ``max_tokens`` in the tree is
    a long-form article's, and four megabytes is roughly forty times what that
    can come back as.
    """
    from app.services import content_generator

    largest_tokens = int(max(content_generator.TARGET_WORDS.values()) * 2.2) + 5000
    # Four bytes per token is generous for English, and the reply carries the
    # prompt's worth of envelope on top; double it and it is still nowhere near.
    generous_bytes = largest_tokens * 4 * 2

    assert generous_bytes * 10 < llm_router.MAX_RESPONSE_BYTES, (
        f"the cap ({llm_router.MAX_RESPONSE_BYTES}) is within reach of a real "
        f"completion (~{generous_bytes} bytes)"
    )
