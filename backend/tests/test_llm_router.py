"""The provider chain and its circuit breaker."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import ai, llm_router


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


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
    assert ai.as_float(5, default=0.5) == 1.0  # clamped
