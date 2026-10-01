"""A retired model is a configuration fault, not an outage.

Every other way a provider can fail here is a wait: a rate limit ends, a 5xx
passes, a transport error was the network. A model that has been retired is
none of those — the provider is up and answering correctly that the name it was
given no longer exists, and it will answer the same way on every sweep until
somebody edits ``.env``.

Pulse had no way to say that. Its Gemini slot answered
``404 … models/gemini-2.0-flash is no longer available`` on every generation
from at least 2026-08-11, logged at ``WARNING`` among the free tiers' ordinary
per-minute rate-limit chatter, and the circuit breaker dutifully cooled it down
and re-asked five minutes later, forever. The provider contributed nothing for
days and nothing in the log said so.

So the classification is asserted here: what makes it distinguishable, what it
must *not* swallow, and — the part with the real regression risk — that
condemning a model does not condemn a provider that still has a working one.
"""
from __future__ import annotations

import logging

import pytest

from app.config import Settings, settings
from app.services import llm_router


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


class _FakeResponse:
    """Just enough of httpx.Response for :func:`llm_router._call`."""

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


#: The body Google actually sends, trimmed. Kept verbatim rather than
#: paraphrased — the classifier matches on this text, so a test that invents its
#: own wording would pass while the real thing went on being missed.
_GEMINI_404 = (
    '[{ "error": { "code": 404, "message": "This model '
    "models/gemini-2.0-flash is no longer available. Please update your code "
    'to use a newer model.", "status": "NOT_FOUND" } }]'
)


@pytest.fixture
def two_providers(monkeypatch):
    """Gemini then Groq, both configured, nothing else."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "key-gemini")
    monkeypatch.setattr(settings, "groq_api_key", "key-groq")
    monkeypatch.setattr(settings, "gemini_fallback_models", "")
    monkeypatch.setattr(settings, "groq_fallback_models", "")


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #


def test_a_retired_model_is_not_read_as_an_ordinary_failure(two_providers, monkeypatch):
    """The 404 body names the model, so it is a configuration fault."""

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse({}, status_code=404, text=_GEMINI_404)
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    # The chain still delivers — this must never be the reason a piece is not
    # written when another provider is up.
    assert result.provider == "groq"
    # And Gemini is now held down rather than re-asked on the next sweep.
    assert llm_router.breaker.is_open("gemini")


def test_a_retired_model_is_reported_at_error_naming_the_setting(
    two_providers, monkeypatch, caplog
):
    """The old line was a WARNING that read like a rate limit.

    An operator scanning the journal has to be able to tell "this will fix
    itself" from "this needs an edit", and be told which edit.
    """

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse({}, status_code=404, text=_GEMINI_404)
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    with caplog.at_level(logging.ERROR, logger="app.services.llm_router"):
        llm_router.complete([{"role": "user", "content": "hi"}])

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a retired model produced no ERROR-level log line"
    assert any("GEMINI_MODEL" in message for message in errors)


def test_the_same_fault_is_caught_when_it_arrives_as_a_200(two_providers, monkeypatch):
    """These compat layers report a bad model as a 200-with-error-body as
    readily as they report a spent quota — see ``_body_error``."""

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse(
                {"error": {"message": "The model `gemini-1.0-pro` does not exist"}}
            )
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    assert result.provider == "groq"
    assert llm_router.breaker.is_open("gemini")


# --------------------------------------------------------------------------- #
# Narrowness — a false positive parks a working provider                       #
# --------------------------------------------------------------------------- #


def test_a_rate_limit_is_still_a_rate_limit(two_providers, monkeypatch):
    """The classifier must not swallow the case it sits next to.

    A 429 is the failure this router spends most of its life handling, and
    reading one as a dead model would hold a healthy provider down for an hour.
    """

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse(
                {},
                status_code=429,
                text='{"error": {"message": "Resource exhausted"}}',
            )
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    llm_router.complete([{"role": "user", "content": "hi"}])

    # One 429 is not three, so the ordinary threshold has not been reached and
    # nothing has claimed the provider is misconfigured.
    assert not llm_router.breaker.is_open("gemini")


def test_a_server_error_mentioning_a_model_is_not_a_retired_model(
    two_providers, monkeypatch
):
    """A 5xx is the provider being broken, whatever its body says.

    Standing it down for the hour-long ceiling on the strength of a phrase in an
    error page would take a provider out of the chain for an outage it was going
    to recover from in a minute.
    """

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse(
                {},
                status_code=503,
                text="upstream model not found in pool; retry",
            )
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    llm_router.complete([{"role": "user", "content": "hi"}])

    assert not llm_router.breaker.is_open("gemini")


# --------------------------------------------------------------------------- #
# A dead model is not a dead provider                                          #
# --------------------------------------------------------------------------- #


def test_a_working_fallback_model_still_serves_and_keeps_its_provider(
    two_providers, monkeypatch
):
    """The regression this fix could most easily have introduced.

    ``*_FALLBACK_MODELS`` exists precisely so a provider survives one of its
    models becoming unusable. Condemning the provider on the first missing model
    would skip the fallback that was configured for this exact case — and would
    do it while the fallback was working.
    """
    monkeypatch.setattr(settings, "gemini_fallback_models", "gemini-flash-latest")
    asked: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        asked.append(json["model"])
        if "generativelanguage" not in url:
            return _FakeResponse(_completion("groq wrote this"))
        if json["model"] == settings.gemini_model:
            return _FakeResponse({}, status_code=404, text=_GEMINI_404)
        return _FakeResponse(_completion("gemini's fallback wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    assert result.provider == "gemini"
    assert result.model == "gemini-flash-latest"
    assert asked == [settings.gemini_model, "gemini-flash-latest"]
    # The provider is fine. Only one of its models was not.
    assert not llm_router.breaker.is_open("gemini")


def test_the_provider_is_stood_down_only_when_every_model_is_gone(
    two_providers, monkeypatch
):
    """Then, and only then, it is the provider that is misconfigured."""
    monkeypatch.setattr(settings, "gemini_fallback_models", "also-retired")

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse({}, status_code=404, text=_GEMINI_404)
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    assert result.provider == "groq"
    assert llm_router.breaker.is_open("gemini")


def test_a_stood_down_provider_is_not_asked_again_on_the_next_call(
    two_providers, monkeypatch
):
    """The point of the long cooldown: one request an hour, not one per sweep.

    This is what the old behaviour got wrong — a five-minute cooldown against a
    fault that resolves in exactly zero of those five minutes meant Pulse asked
    a dead provider for a model it did not have on every single generation.
    """
    urls: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        urls.append(url)
        if "generativelanguage" in url:
            return _FakeResponse({}, status_code=404, text=_GEMINI_404)
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    llm_router.complete([{"role": "user", "content": "hi"}])
    first_round = len([u for u in urls if "generativelanguage" in u])
    llm_router.complete([{"role": "user", "content": "hi again"}])

    assert first_round == 1
    assert len([u for u in urls if "generativelanguage" in u]) == 1


def test_the_shipped_gemini_default_is_not_a_pinned_name():
    """``gemini-2.0-flash`` was the default and Google retired it.

    A pinned version in the shipped default means every install inherits a model
    that will one day stop existing. The alias is the value that does not.
    """
    assert Settings.model_fields["gemini_model"].default == "gemini-flash-latest"
