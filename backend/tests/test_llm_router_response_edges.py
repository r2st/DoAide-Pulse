"""The shapes a provider's *response* can take that the happy path never sees.

tests/test_llm_router.py is about the chain: who gets asked, in what order,
and what the breaker does afterwards. This module is about the single call —
:func:`app.services.llm_router._call` and the small parsers around it — where a
200 can still be a failure and a header can still be a lie. Every one of these
is a real free-tier response shape, not a hypothetical: a truncated completion,
a reasoning model that returns only whitespace, an HTML error page served with
a 200, and a ``Retry-After`` written as a bare date with no zone.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.services import llm_router


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


class _Resp:
    """Just enough of ``httpx.Response`` for :func:`llm_router._call`."""

    def __init__(self, payload, *, status_code=200, headers=None, text="", raises=None):
        self._payload = payload
        self._raises = raises
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


def _provider() -> llm_router.Provider:
    return llm_router.Provider(
        name="openrouter",
        api_key="key",
        base_url="https://openrouter.ai/api/v1",
        model="primary/model",
    )


def _call(monkeypatch, resp) -> str:
    """The text ``_call`` produced, for the tests below that are about the text.

    ``_call`` returns a ``_Served`` — text plus the provider's raw ``usage``
    block, which :mod:`app.services.llm_usage` needs and none of the assertions
    in this module are about. Unwrapped here so those assertions stay about the
    one thing they were written for; the usage half is pinned in
    ``test_llm_usage_is_recorded.py``.
    """
    monkeypatch.setattr(llm_router, "_post", lambda *a, **k: resp)
    return llm_router._call(
        _provider(),
        [{"role": "user", "content": "hi"}],
        model="primary/model",
        temperature=0.7,
        max_tokens=100,
        timeout=5.0,
    ).text


# --------------------------------------------------------------------------- #
# Retry-After                                                                 #
# --------------------------------------------------------------------------- #


def test_a_retry_after_date_without_a_zone_is_read_as_utc():
    """RFC 9110 requires GMT, and some providers omit the zone anyway.

    ``parsedate_to_datetime`` hands back a *naive* datetime for those, and
    subtracting a naive datetime from an aware one raises TypeError — which
    would turn a parseable header into a 500 inside the error handler. The
    zone-less form has to be assumed UTC, which is what the RFC means anyway.
    """
    later = datetime.now(UTC) + timedelta(seconds=120)
    naive = later.strftime("%a, %d %b %Y %H:%M:%S")

    wait = llm_router._retry_after({"Retry-After": naive})

    assert wait is not None
    assert 100 < wait < 130


def test_a_zoneless_retry_after_in_the_past_reads_as_no_wait():
    gone = datetime.now(UTC) - timedelta(hours=2)

    assert llm_router._retry_after(
        {"Retry-After": gone.strftime("%a, %d %b %Y %H:%M:%S")}
    ) == 0.0


# --------------------------------------------------------------------------- #
# 200-with-an-error-body                                                      #
# --------------------------------------------------------------------------- #


def test_a_200_error_body_that_is_not_a_rate_limit_is_a_plain_failure():
    """Not every ``error`` body is a spent quota.

    Reading a generic upstream failure as a rate limit would open the breaker
    for twenty seconds and cost the chain a provider that is up and would answer
    immediately.
    """
    error = llm_router._body_error(
        _provider(), {"error": {"message": "internal upstream failure", "code": 500}}
    )

    assert type(error) is llm_router.LLMError
    assert not isinstance(error, llm_router.LLMRateLimited)
    assert "internal upstream failure" in str(error)


def test_a_200_error_body_naming_a_missing_model_is_its_own_kind_of_failure():
    """Sharper than the case above, and for a reason the breaker cares about.

    "model not found" is still not a rate limit — but it is also not a failure
    that waiting fixes, so it gets its own class rather than being lumped in
    with the transient ones. See
    :class:`app.services.llm_router.LLMModelUnavailable`.
    """
    error = llm_router._body_error(
        _provider(), {"error": {"message": "model not found", "code": 404}}
    )

    assert isinstance(error, llm_router.LLMModelUnavailable)
    assert not isinstance(error, llm_router.LLMRateLimited)
    assert "model not found" in str(error)


def test_a_200_error_body_with_no_message_still_names_the_provider():
    error = llm_router._body_error(_provider(), {"error": "a bare string"})

    assert type(error) is llm_router.LLMError
    assert "openrouter" in str(error)
    assert "no choices returned" in str(error)


def test_a_rate_limit_code_in_an_error_body_carries_its_header_wait():
    error = llm_router._body_error(
        _provider(),
        {
            "error": {
                "message": "slow down",
                "code": 429,
                "metadata": {"headers": {"Retry-After": "45"}},
            }
        },
    )

    assert isinstance(error, llm_router.LLMRateLimited)
    assert error.retry_after == 45.0


# --------------------------------------------------------------------------- #
# Malformed 200s                                                              #
# --------------------------------------------------------------------------- #


def test_a_body_that_is_not_json_names_itself_as_such(monkeypatch):
    """A gateway's HTML error page arrives with a 200 more often than it should.

    "not JSON" has to survive as its own message: the caller's next guess is
    otherwise that the *model* wrote something unparseable, which sends whoever
    is debugging it into the prompt instead of the gateway.
    """
    resp = _Resp(None, raises=ValueError("Expecting value: line 1 column 1"))

    with pytest.raises(llm_router.LLMError) as excinfo:
        _call(monkeypatch, resp)

    assert "not JSON" in str(excinfo.value)
    assert "openrouter" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"choices": []}, id="empty-choices"),
        pytest.param({"choices": [{}]}, id="choice-without-message"),
        pytest.param({"choices": "not-a-list"}, id="choices-not-a-list"),
        pytest.param({"choices": [None]}, id="null-choice"),
    ],
)
def test_a_choices_key_that_does_not_hold_a_choice_is_an_unexpected_payload(
    monkeypatch, payload
):
    """``choices`` is present, so :func:`_body_error` does not fire — but there
    is still nothing to read. Each of these indexes or attributes its way into a
    different exception type, and all three have to land as one LLMError."""
    with pytest.raises(llm_router.LLMError) as excinfo:
        _call(monkeypatch, _Resp(payload))

    assert "unexpected payload" in str(excinfo.value)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"content": ""}, id="empty-string"),
        pytest.param({"content": None, "reasoning": None}, id="both-null"),
        pytest.param({"content": "   \n\t  "}, id="whitespace-only"),
        pytest.param({"content": {"parts": ["x"]}}, id="content-not-a-string"),
    ],
)
def test_a_completion_with_no_usable_text_is_an_error_not_an_empty_string(
    monkeypatch, message
):
    """Returning "" here would be worse than raising.

    The caller's next step is to parse the text as JSON and fall back to a
    static template on failure — so an empty string reads as "the model wrote
    something bad" and the provider chain never advances to one that works.
    """
    payload = {"choices": [{"message": message, "finish_reason": "stop"}]}

    with pytest.raises(llm_router.LLMError, match="empty completion"):
        _call(monkeypatch, _Resp(payload))


def test_a_reasoning_model_that_parks_the_answer_in_reasoning_is_read(monkeypatch):
    payload = {
        "choices": [
            {"message": {"content": None, "reasoning": "  the answer  "},
             "finish_reason": "stop"}
        ]
    }

    assert _call(monkeypatch, _Resp(payload)) == "the answer"


# --------------------------------------------------------------------------- #
# Truncation                                                                  #
# --------------------------------------------------------------------------- #


def test_a_truncated_completion_is_returned_but_logged_with_the_numbers(
    monkeypatch, caplog
):
    """``finish_reason: length`` is the free tier's most confusing failure.

    The text comes back — it is just cut off mid-JSON — so the caller sees "no
    parseable JSON" and blames the prompt. The warning has to name the token
    budget and the reasoning spend, because that is the pair that has to change.
    """
    payload = {
        "choices": [
            {"message": {"content": '{"title": "half a str'}, "finish_reason": "length"}
        ],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 964}},
    }

    with caplog.at_level("WARNING", logger="app.services.llm_router"):
        text = _call(monkeypatch, _Resp(payload))

    assert text == '{"title": "half a str'
    assert "max_tokens=100" in caplog.text
    assert "reasoning_tokens=964" in caplog.text


def test_a_truncated_completion_without_usage_still_warns(monkeypatch, caplog):
    """No ``usage`` block is the common case on the non-reasoning models — the
    warning still has to fire, with the one number it does have."""
    payload = {
        "choices": [{"message": {"content": "cut off"}, "finish_reason": "length"}]
    }

    with caplog.at_level("WARNING", logger="app.services.llm_router"):
        assert _call(monkeypatch, _Resp(payload)) == "cut off"

    assert "reasoning_tokens=None" in caplog.text


# --------------------------------------------------------------------------- #
# The model chain                                                             #
# --------------------------------------------------------------------------- #


def test_a_fallback_that_repeats_the_primary_is_not_tried_twice():
    """Config drift puts the primary in the fallback list often enough.

    Left in, the chain spends two of its attempts on the same spent quota — and
    the second one is charged against the breaker as if it were a second model.
    """
    provider = llm_router.Provider(
        name="openrouter",
        api_key="key",
        base_url="https://openrouter.ai/api/v1",
        model="a/model",
        fallback_models=("a/model", "b/model", "b/model", ""),
    )

    assert llm_router._model_chain(provider, None) == ["a/model", "b/model"]


def test_a_caller_override_that_duplicates_a_fallback_collapses():
    provider = llm_router.Provider(
        name="openrouter",
        api_key="key",
        base_url="https://openrouter.ai/api/v1",
        model="a/model",
        fallback_models=("b/model",),
    )

    assert llm_router._model_chain(provider, "b/model", ("b/model", "c/model")) == [
        "b/model",
        "c/model",
    ]


# --------------------------------------------------------------------------- #
# The pause between sweeps                                                    #
# --------------------------------------------------------------------------- #


def test_a_zero_backoff_ceiling_means_no_second_sweep(monkeypatch):
    """``llm_retry_max_backoff_seconds = 0`` is how an operator turns the
    between-sweep wait off. The clamp takes the pause to zero, and a zero-length
    sleep followed by an identical sweep is pure waste — so there is no second
    sweep at all."""
    monkeypatch.setattr(settings, "llm_retry_max_backoff_seconds", 0)
    outcome = llm_router._Pass(
        errors=["openrouter rate-limited"],
        rate_limit_waits=[30.0],
        other_failure=False,
    )

    assert llm_router._pause_before_retrying(outcome, [_provider()]) is None


def test_the_sleep_indirection_actually_sleeps():
    """The rest of the suite patches :func:`llm_router._sleep` away, which
    leaves the one line that makes it real unexercised — and a typo there is a
    worker that never pauses and hammers a rate-limited provider."""
    started = time.monotonic()

    llm_router._sleep(0.01)

    assert time.monotonic() - started >= 0.01
