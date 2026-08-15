"""The provider chain and its circuit breaker."""
from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.services import ai, llm_router


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


# --------------------------------------------------------------------------- #
# Fallback plumbing                                                           #
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """Just enough of httpx.Response for :func:`llm_router._call`.

    Carries a status and headers as well as a body: the router reads the status
    line to tell "come back later" from "never", and ``Retry-After`` to tell a
    per-minute limit from a spent daily quota.
    """

    def __init__(
        self,
        payload: dict,
        *,
        status_code: int = 200,
        headers: dict | None = None,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _completion(text: str) -> dict:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


@pytest.fixture
def all_keys(monkeypatch):
    """OpenRouter → Gemini → Groq, all configured."""
    monkeypatch.setattr(settings, "openrouter_api_key", "key-openrouter")
    monkeypatch.setattr(settings, "gemini_api_key", "key-gemini")
    monkeypatch.setattr(settings, "groq_api_key", "key-groq")
    monkeypatch.setattr(settings, "cerebras_api_key", "")


def test_the_fallback_chain_is_openrouter_then_gemini_then_groq(all_keys):
    assert llm_router.configured_providers() == ["openrouter", "gemini", "groq"]


def test_chain_falls_through_to_groq_when_the_first_two_fail(all_keys, monkeypatch):
    """The case that matters in production.

    OpenRouter's free tier is 50 requests/day shared across every app using the
    key; past that it answers 200 with an `error` body rather than a non-2xx.
    Gemini here fails at the transport instead, so both failure shapes are
    covered in one pass.
    """
    calls: list[tuple[str, str, str]] = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json["model"], headers["Authorization"]))
        if "openrouter.ai" in url:
            return _FakeResponse(
                {"error": {"message": "Rate limit exceeded: free-models-per-day"}}
            )
        if "generativelanguage" in url:
            raise httpx.ConnectError("no route to host")
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "groq"
    assert result.text == "groq wrote this"
    assert result.model == settings.groq_model

    # Every provider was tried in order, each with its own key and model —
    # a chain that sent one provider's model name to another would 400.
    assert [c[1] for c in calls] == [
        settings.openrouter_model,
        settings.gemini_model,
        settings.groq_model,
    ]
    assert [c[2] for c in calls] == [
        "Bearer key-openrouter",
        "Bearer key-gemini",
        "Bearer key-groq",
    ]


def test_a_per_call_model_override_does_not_leak_to_the_fallbacks(all_keys, monkeypatch):
    """``openai/gpt-oss-120b:free`` means nothing to Groq — it would 400."""
    models: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        models.append(json["model"])
        if "openrouter.ai" in url:
            raise httpx.ConnectError("down")
        return _FakeResponse(_completion("fallback text"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    llm_router.complete(
        [{"role": "user", "content": "hi"}],
        model=settings.openrouter_long_form_model,
    )
    assert models[0] == settings.openrouter_long_form_model
    assert models[1] == settings.gemini_model


def test_an_open_breaker_skips_a_provider_without_calling_it(all_keys, monkeypatch):
    for _ in range(settings.llm_breaker_threshold):
        llm_router.breaker.record_failure("openrouter")
    assert llm_router.breaker.is_open("openrouter")

    urls: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        urls.append(url)
        return _FakeResponse(_completion("gemini wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "gemini"
    assert not any("openrouter.ai" in u for u in urls)


def test_every_provider_failing_names_every_provider(all_keys, monkeypatch):
    monkeypatch.setattr(
        llm_router,
        "_post",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    with pytest.raises(llm_router.AllProvidersFailed) as exc:
        llm_router.complete([{"role": "user", "content": "hi"}])
    message = str(exc.value)
    for name in ("openrouter", "gemini", "groq"):
        assert name in message


def test_a_lone_fallback_key_is_enough_to_generate(monkeypatch):
    """No OpenRouter key at all: Groq alone must serve the request."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "key-groq")
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **kw: _FakeResponse(_completion("hi"))
    )
    assert llm_router.complete([{"role": "user", "content": "x"}]).provider == "groq"


def test_no_keys_means_the_chain_gives_up_immediately():
    with pytest.raises(llm_router.AllProvidersFailed) as exc:
        llm_router.complete([{"role": "user", "content": "hi"}])
    assert "No LLM provider is configured" in str(exc.value)


def test_ai_error_wraps_chain_failure():
    with pytest.raises(ai.AIError):
        ai.chat_completion([{"role": "user", "content": "hi"}])


def test_providers_are_ordered_and_keyless_ones_skipped(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "k1")
    monkeypatch.setattr(settings, "groq_api_key", "k2")
    assert llm_router.configured_providers() == ["openrouter", "groq"]


def test_breaker_trips_after_consecutive_failures():
    breaker = llm_router.CircuitBreaker(threshold=3, cooldown_seconds=60)
    assert breaker.record_failure("x") is False
    assert breaker.record_failure("x") is False
    assert breaker.record_failure("x") is True
    assert breaker.is_open("x")


def test_a_success_resets_the_count():
    breaker = llm_router.CircuitBreaker(threshold=2, cooldown_seconds=60)
    breaker.record_failure("x")
    breaker.record_success("x")
    # Back to zero — the next failure must not trip it.
    assert breaker.record_failure("x") is False


def test_breaker_reopens_after_the_cooldown():
    breaker = llm_router.CircuitBreaker(threshold=1, cooldown_seconds=10)
    breaker.record_failure("x", now=100.0)
    assert breaker.is_open("x", now=105.0)
    assert not breaker.is_open("x", now=111.0)


def test_extract_json_survives_fences_and_scratchpad():
    raw = 'We need to answer.\n```json\n{"title": "A", "n": 2}\n```\nDone.'
    assert ai.extract_json_object(raw) == {"title": "A", "n": 2}
    assert ai.extract_json_object("no json here") is None


def test_extract_json_ignores_braces_inside_strings():
    """A code snippet in body_markdown must not throw off the brace count.

    The naive first-`{`-to-last-`}` span this replaced would end on the brace
    inside the snippet and parse as nothing.
    """
    raw = '{"body_markdown": "Use `{\\"a\\": 1}` here", "title": "T"}'
    assert ai.extract_json_object(raw) == {
        "body_markdown": 'Use `{"a": 1}` here',
        "title": "T",
    }


def test_extract_json_picks_the_largest_object_from_a_scratchpad():
    """Reasoning models sketch before answering; the finished object is bigger."""
    raw = (
        'We need a post. Plan: {"title": "?"} '
        'Now the answer: {"title": "Herald ships", "body_markdown": "## Why\\n\\nBecause."}'
    )
    parsed = ai.extract_json_object(raw)
    assert parsed["title"] == "Herald ships"
    assert "body_markdown" in parsed


def test_extract_json_returns_none_for_a_truncated_object():
    """A response cut off at max_tokens has no closing brace and must not
    half-parse into something that looks like a usable post."""
    raw = '{"title": "Herald ships", "body_markdown": "## Why\\n\\nBecause we ne'
    assert ai.extract_json_object(raw) is None


def test_json_completion_reports_unparseable_output_as_an_ai_error(monkeypatch):
    from app.services import llm_router

    monkeypatch.setattr(
        llm_router,
        "complete",
        lambda *a, **kw: llm_router.Completion(
            text="I have thoughts but no JSON.", provider="fake", model="fake-1"
        ),
    )
    with pytest.raises(ai.AIError) as exc:
        ai.json_completion([{"role": "user", "content": "hi"}])
    assert "no parseable JSON" in str(exc.value)


def test_reasoning_detection_is_conservative():
    assert ai.looks_like_reasoning("We need to write an announcement about Herald.")
    assert ai.looks_like_reasoning("The user wants a post. Let's write one.")
    # One incidental marker in real prose must not trip it.
    assert not ai.looks_like_reasoning(
        "Herald ships a lot. We must be careful about rate limits, though."
    )
    assert not ai.looks_like_reasoning("")


def test_coercion_helpers_tolerate_loose_model_output():
    assert ai.as_str(42) == "42"
    assert ai.as_str(["a", "b"]) == "a b"
    assert ai.as_str_list("python, ai, python") == ["python", "ai"]
    assert ai.as_str_list(["a"] * 10, limit=3) == ["a"]
    assert ai.as_float("0.9", default=0.5) == 0.9
    assert ai.as_float("not a number", default=0.5) == 0.5
    # Off the scale is not the top of the scale: see `as_float`. A model that
    # answered 5 to a 0.0-1.0 question was answering a different question.
    assert ai.as_float(5, default=0.5) == 0.5


# --------------------------------------------------------------------------- #
# Rate limits                                                                  #
# --------------------------------------------------------------------------- #
#
# The free tiers meter two different things and the two want opposite answers:
# a per-minute limit clears in seconds and is worth waiting out, a per-day quota
# does not clear today and is worth standing down from. Getting this wrong is
# what turns a rate limit into a confidence-0 template and a blocked
# auto-publish, which is the whole reason this section exists.


@pytest.fixture
def no_sleeping(monkeypatch):
    """Record what the chain would have slept for instead of sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(llm_router, "_sleep", slept.append)
    return slept


def _rate_limited(retry_after: str | None = None) -> _FakeResponse:
    """A real 429, with the platform's own answer to "when?"."""
    headers = {"Retry-After": retry_after} if retry_after else {}
    return _FakeResponse({}, status_code=429, headers=headers, text="slow down")


def test_a_429_does_not_delay_a_provider_that_would_have_worked(
    all_keys, no_sleeping, monkeypatch
):
    """The ordering rule: sweep the whole chain before sleeping on any of it.

    OpenRouter refusing is not a reason to make the caller wait when Gemini is
    sitting there ready to answer.
    """

    def fake_post(url, *, json, headers, timeout):
        if "openrouter.ai" in url:
            return _rate_limited("30")
        return _FakeResponse(_completion("gemini wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "gemini"
    assert no_sleeping == []


def test_a_whole_chain_of_429s_is_waited_out_rather_than_given_up_on(
    all_keys, no_sleeping, monkeypatch
):
    """The case the user actually hits: every free tier refusing at once.

    Falling through to the caller's template here produces a draft with
    confidence 0.0, which the autopilot will never publish. A per-minute limit
    clears in seconds, so one wait is the difference between a published post
    and a blocked auto-publish.
    """
    attempts = {"n": 0}

    def fake_post(url, *, json, headers, timeout):
        attempts["n"] += 1
        # Everything refuses on the first sweep; the second one goes through.
        if attempts["n"] <= 3:
            return _rate_limited("5")
        return _FakeResponse(_completion("second time lucky"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])
    assert result.text == "second time lucky"
    assert len(no_sleeping) == 1
    # Never longer than the shortest wait a refusing provider named.
    assert 0 < no_sleeping[0] <= 5


def test_the_wait_is_the_shortest_one_any_provider_named(
    all_keys, no_sleeping, monkeypatch
):
    """Whoever frees up first is when there is a point in asking again."""
    waits = iter(["600", "5", "300"])

    def fake_post(url, *, json, headers, timeout):
        return _rate_limited(next(waits, "300"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])
    assert no_sleeping
    assert all(0 < wait <= 5 for wait in no_sleeping)


def test_a_rate_limit_with_no_retry_after_is_treated_as_a_per_minute_one(
    all_keys, no_sleeping, monkeypatch
):
    """Guessing wrong in this direction costs one short nap; the other costs a post."""
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **kw: _rate_limited(None)
    )
    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])
    assert no_sleeping
    assert all(0 < wait <= llm_router._DEFAULT_RATE_LIMIT_PAUSE for wait in no_sleeping)


def test_a_failure_that_is_not_a_rate_limit_is_not_slept_on(
    all_keys, no_sleeping, monkeypatch
):
    """A dead host does not become reachable because we waited thirty seconds."""
    monkeypatch.setattr(
        llm_router,
        "_post",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])
    assert no_sleeping == []


def test_a_bad_key_is_not_retried(all_keys, no_sleeping, monkeypatch):
    """A 401 is the provider saying "never", not "later"."""
    calls: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        calls.append(url)
        return _FakeResponse({}, status_code=401, text="invalid api key")

    monkeypatch.setattr(llm_router, "_post", fake_post)

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])
    # One request per provider and no more: three keys, three calls.
    assert len(calls) == 3
    assert no_sleeping == []


def test_openrouters_200_with_a_rate_limit_body_is_read_as_a_rate_limit(
    all_keys, no_sleeping, monkeypatch
):
    """OpenRouter's free tier answers a spent quota with HTTP 200.

    Read as a plain failure this costs the chain a provider it could come back
    to, and costs the request the one wait that would have rescued it.
    """
    attempts = {"n": 0}

    def fake_post(url, *, json, headers, timeout):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            return _FakeResponse(
                {"error": {"message": "Rate limit exceeded: free-models-per-day"}}
            )
        return _FakeResponse(_completion("after the wait"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    assert llm_router.complete([{"role": "user", "content": "hi"}]).text == (
        "after the wait"
    )
    assert len(no_sleeping) == 1


def test_a_long_retry_after_stands_the_provider_down_for_that_long(
    all_keys, no_sleeping, monkeypatch
):
    """A spent daily quota should cost one refused request, not one per sweep.

    Without this the breaker's three-consecutive-failures rule keeps asking a
    provider that has already said, in seconds, when it will next say yes.
    """
    monkeypatch.setattr(
        llm_router,
        "_post",
        lambda *a, **kw: _rate_limited("1800"),
    )
    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    # One refusal was enough — no waiting for the failure threshold.
    assert llm_router.breaker.is_open("openrouter")
    assert llm_router.breaker.is_open("gemini")
    assert llm_router.breaker.is_open("groq")


def test_a_stood_down_provider_is_not_swept_again(all_keys, no_sleeping, monkeypatch):
    """Nothing to gain from a second sweep when every provider is standing down."""
    calls: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        calls.append(url)
        return _rate_limited("1800")

    monkeypatch.setattr(llm_router, "_post", fake_post)

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])
    assert len(calls) == 3


def test_the_breaker_cooldown_from_a_retry_after_is_capped(all_keys, monkeypatch):
    """A provider asking for a week does not get a week."""
    monkeypatch.setattr(settings, "llm_breaker_max_cooldown_seconds", 60)
    monkeypatch.setattr(llm_router, "_sleep", lambda _s: None)
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **kw: _rate_limited("604800")
    )
    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    snapshot = llm_router.breaker.snapshot()
    assert snapshot["openrouter"]["seconds_until_retry"] <= 60


def test_retry_after_accepts_an_http_date(monkeypatch):
    """RFC 9110 allows a date as well as a delay, and providers send both."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    later = datetime.now(UTC) + timedelta(seconds=120)
    assert 100 < llm_router._retry_after({"Retry-After": format_datetime(later)}) < 130
    # A date already gone reads as no guidance, not as a negative wait.
    gone = datetime.now(UTC) - timedelta(seconds=120)
    assert llm_router._retry_after({"Retry-After": format_datetime(gone)}) == 0.0
    assert llm_router._retry_after({}) is None
    assert llm_router._retry_after({"Retry-After": "not a date"}) is None


# --------------------------------------------------------------------------- #
# Model fallback within one provider                                           #
# --------------------------------------------------------------------------- #


def test_a_rate_limited_model_falls_back_to_its_sibling_on_the_same_key(
    all_keys, no_sleeping, monkeypatch
):
    """Free-tier quota is metered per model, so the sibling is a real second go.

    And it is a *better* second go than the next provider, whose key may not be
    configured at all.
    """
    monkeypatch.setattr(settings, "openrouter_fallback_models", "openai/other:free")
    models: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        models.append(json["model"])
        if json["model"] == settings.openrouter_model:
            # No Retry-After: a per-model minute limit, not a spent day.
            return _rate_limited(None)
        return _FakeResponse(_completion("the sibling answered"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "openrouter"
    assert result.model == "openai/other:free"
    assert models == [settings.openrouter_model, "openai/other:free"]
    # The sibling answered on the first sweep, so nobody waited.
    assert no_sleeping == []


def test_a_caller_can_name_its_own_sibling_model(all_keys, no_sleeping, monkeypatch):
    """What content_generator does: the long-form and short-form models are
    each other's fallback, and both are already configured and free."""
    models: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        models.append(json["model"])
        if json["model"] == settings.openrouter_model:
            return _rate_limited(None)
        return _FakeResponse(_completion("the other model answered"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete(
        [{"role": "user", "content": "hi"}],
        model=settings.openrouter_model,
        fallback_models=(settings.openrouter_long_form_model,),
    )
    assert result.model == settings.openrouter_long_form_model
    assert models[:2] == [
        settings.openrouter_model,
        settings.openrouter_long_form_model,
    ]


def test_a_caller_named_sibling_does_not_leak_to_another_provider(
    all_keys, no_sleeping, monkeypatch
):
    """An OpenRouter model id means nothing to Gemini — it would 400."""
    models: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        models.append(json["model"])
        if "openrouter.ai" in url:
            return _rate_limited(None)
        return _FakeResponse(_completion("gemini wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete(
        [{"role": "user", "content": "hi"}],
        model=settings.openrouter_model,
        fallback_models=(settings.openrouter_long_form_model,),
    )
    assert result.provider == "gemini"
    assert models[-1] == settings.gemini_model


def test_a_spent_daily_quota_skips_the_sibling_too(all_keys, no_sleeping, monkeypatch):
    """A key-wide daily limit is not a per-model one — do not spend a request
    proving that on every model in the chain."""
    monkeypatch.setattr(settings, "openrouter_fallback_models", "openai/other:free")
    models: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        models.append(json["model"])
        if "openrouter.ai" in url:
            return _rate_limited("3600")
        return _FakeResponse(_completion("gemini wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    assert llm_router.complete([{"role": "user", "content": "hi"}]).provider == "gemini"
    assert models.count(settings.openrouter_model) == 1
    assert "openai/other:free" not in models


def test_the_sweep_budget_is_spent_exactly_and_not_slept_off_at_the_end(
    all_keys, no_sleeping, monkeypatch
):
    """``llm_max_attempts`` sweeps, and ``llm_max_attempts - 1`` waits.

    The guard that stops after the last sweep is the loop's only bound, so it
    has to be exact in both directions. One sweep too few wastes a provider
    that was about to free up; one wait too many parks the caller for a
    rate-limit window before handing back the failure it already knows about —
    which is the difference between a static-template fallback that renders
    now and one that renders a minute from now.
    """
    # Two, not the default three, so this asserts the setting is read rather
    # than that a constant happens to match.
    monkeypatch.setattr(settings, "llm_max_attempts", 2)
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **kw: _rate_limited("5")
    )

    # Count passes over the chain rather than HTTP calls: once the breaker
    # stands a provider down its sweep makes no request, and a sweep that
    # skipped every provider still spent a pass.
    real_sweep = llm_router._sweep
    sweeps = {"n": 0}

    def counted(*args, **kwargs):
        sweeps["n"] += 1
        return real_sweep(*args, **kwargs)

    monkeypatch.setattr(llm_router, "_sweep", counted)

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    assert sweeps["n"] == 2
    assert len(no_sleeping) == 1


def test_a_single_attempt_budget_never_waits(all_keys, no_sleeping, monkeypatch):
    """``llm_max_attempts=1`` means one pass and no nap before giving up."""
    monkeypatch.setattr(settings, "llm_max_attempts", 1)
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **kw: _rate_limited("5")
    )

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    assert no_sleeping == []


def test_a_nonsense_attempt_budget_still_makes_one_pass(
    all_keys, no_sleeping, monkeypatch
):
    """``max(1, ...)`` floors it: 0 or a negative must not mean "never call".

    Falling through with no request at all would report every provider as
    failed while none had been asked.
    """
    monkeypatch.setattr(settings, "llm_max_attempts", 0)
    calls = {"n": 0}

    def fake_post(url, *, json, headers, timeout):
        calls["n"] += 1
        return _rate_limited("5")

    monkeypatch.setattr(llm_router, "_post", fake_post)

    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    assert calls["n"] == 3
    assert no_sleeping == []
