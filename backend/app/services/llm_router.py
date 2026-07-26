"""Multi-provider LLM chain — OpenRouter → Gemini → Groq → Cerebras → caller's template.

Every AI feature in Herald (post drafting, headline generation, SEO metadata,
per-platform blurbs) funnels through :func:`complete`. The point is that one
flaky upstream must never turn into a silent product failure: a draft either
gets written by *some* model, or the caller falls back to a deterministic
template — but it is never empty and never a stack trace.

Three things make that work:

* **One dialect.** All four providers speak OpenAI's ``/chat/completions``, so
  they differ only in base URL, key and model name. A provider with no API key
  configured is skipped rather than attempted-and-failed.
* **A circuit breaker.** After ``llm_breaker_threshold`` consecutive failures a
  provider is skipped for ``llm_breaker_cooldown_seconds``. Without it a dead
  upstream costs a full timeout on *every* request, and the chain's latency is
  the sum of everything broken ahead of the one that works. The first success
  resets the count.
* **A terminal template tier.** When every provider is exhausted the chain
  raises :class:`AllProvidersFailed` rather than returning generic filler. Each
  call site owns a *specific* fallback that beats anything this module could
  invent, and the chain's job is to say "you're on your own now".

The breaker state is per-process, in-memory. With several workers that means
each learns about a dead provider independently — fine, since the cost of
learning is one timeout and the alternative (shared state in Redis) buys little
for how rarely this fires.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A single provider call failed."""


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible chat endpoint."""

    name: str
    api_key: str
    base_url: str
    model: str
    # OpenRouter wants attribution headers; nobody else needs extras.
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }


def _providers() -> list[Provider]:
    """The chain, in priority order, skipping anything without a key.

    Read fresh on every call so tests (and a restarted worker picking up new
    env) see key changes without module reloading.
    """
    candidates = [
        Provider(
            name="openrouter",
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            extra_headers={
                "HTTP-Referer": settings.openrouter_app_url,
                "X-Title": settings.openrouter_app_title,
            },
        ),
        Provider(
            name="gemini",
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
        ),
        Provider(
            name="groq",
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            model=settings.groq_model,
        ),
        Provider(
            name="cerebras",
            api_key=settings.cerebras_api_key,
            base_url=settings.cerebras_base_url,
            model=settings.cerebras_model,
        ),
    ]
    return [p for p in candidates if p.api_key]


def configured_providers() -> list[str]:
    """Names of the providers that have a key, in the order they'd be tried."""
    return [p.name for p in _providers()]


# --------------------------------------------------------------------------- #
# Circuit breaker                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class _BreakerState:
    failures: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    """Per-provider consecutive-failure counter with a cool-down.

    Deliberately trips on *consecutive* failures: an upstream that fails one
    request in ten is degraded, not down, and tripping on a cumulative count
    would eventually take it out of rotation permanently.
    """

    def __init__(self, threshold: int, cooldown_seconds: float) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._state: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def is_open(self, name: str, *, now: float | None = None) -> bool:
        """True when *name* should be skipped right now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(name)
            return state is not None and state.open_until > now

    def record_failure(self, name: str, *, now: float | None = None) -> bool:
        """Count a failure; return True if this one tripped the breaker."""
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.failures += 1
            if state.failures >= self.threshold:
                state.open_until = now + self.cooldown
                state.failures = 0
                return True
            return False

    def record_success(self, name: str) -> None:
        with self._lock:
            self._state.pop(name, None)

    def reset(self) -> None:
        """Clear all state — used by tests and after a config change."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current state, for the health endpoint / debugging."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "failures": state.failures,
                    "seconds_until_retry": max(0.0, state.open_until - now),
                }
                for name, state in self._state.items()
            }


breaker = CircuitBreaker(
    threshold=settings.llm_breaker_threshold,
    cooldown_seconds=float(settings.llm_breaker_cooldown_seconds),
)


# --------------------------------------------------------------------------- #
# The chain                                                                    #
# --------------------------------------------------------------------------- #


class AllProvidersFailed(RuntimeError):
    """Every configured provider failed or was skipped.

    Callers should fall back to their own deterministic template.
    :func:`app.services.ai.chat_completion` converts this into ``AIError`` so
    the existing handlers at every call site keep working unchanged.
    """


@dataclass(frozen=True)
class Completion:
    """A finished completion plus which provider actually served it."""

    text: str
    provider: str
    model: str


def _call(
    provider: Provider,
    messages: list[dict[str, str]],
    *,
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """One provider attempt. Raises :class:`LLMError` on any failure."""
    payload = {
        # An explicit per-call model override only makes sense for the provider
        # it was written for; everyone else gets their own configured model.
        "model": model if (model and provider.name == "openrouter") else provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(
            provider.url, json=payload, headers=provider.headers(), timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"{provider.name} request failed: {exc}") from exc

    # OpenRouter (and Gemini's compat layer) report upstream errors as a 200
    # with an `error` body rather than a non-2xx status.
    if isinstance(data, dict) and "choices" not in data:
        detail = (data.get("error") or {}).get("message", "no choices returned")
        raise LLMError(f"{provider.name} request failed: {detail}")

    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{provider.name} returned an unexpected payload: {exc}") from exc

    # A response cut off at max_tokens is the single most confusing failure the
    # free tier produces: the JSON envelope ends mid-string, so the caller sees
    # "no parseable JSON" and blames the model's instruction-following. Say what
    # actually happened, loudly, with the numbers needed to fix it.
    if choice.get("finish_reason") == "length":
        usage = data.get("usage") or {}
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        )
        logger.warning(
            "%s truncated the response at max_tokens=%s (reasoning_tokens=%s) — "
            "the output is incomplete. Raise the caller's token budget.",
            provider.name,
            payload["max_tokens"],
            reasoning_tokens,
        )

    # The free reasoning models park the answer in `reasoning` with a null
    # `content` — see the app.services.ai docstring.
    text = message.get("content") or message.get("reasoning") or ""
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"{provider.name} returned an empty completion")
    return text.strip()


def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> Completion:
    """Try each configured provider in order; return the first success.

    Raises :class:`AllProvidersFailed` when none of them produce usable text —
    the signal for the caller to use its own static template.
    """
    providers = _providers()
    if not providers:
        raise AllProvidersFailed(
            "No LLM provider is configured — set OPENROUTER_API_KEY, "
            "GEMINI_API_KEY, GROQ_API_KEY or CEREBRAS_API_KEY"
        )

    errors: list[str] = []
    for provider in providers:
        if breaker.is_open(provider.name):
            logger.debug("llm provider %s skipped (breaker open)", provider.name)
            errors.append(f"{provider.name}: skipped, circuit open")
            continue

        started = time.monotonic()
        try:
            text = _call(
                provider,
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except LLMError as exc:
            tripped = breaker.record_failure(provider.name)
            logger.warning(
                "llm provider %s failed (%s)%s",
                provider.name,
                exc,
                " — circuit opened" if tripped else "",
            )
            errors.append(str(exc))
            continue

        breaker.record_success(provider.name)
        logger.info(
            "llm served by %s (%s) in %.2fs",
            provider.name,
            provider.model,
            time.monotonic() - started,
        )
        return Completion(text=text, provider=provider.name, model=provider.model)

    raise AllProvidersFailed("All LLM providers failed: " + "; ".join(errors))


__all__ = [
    "AllProvidersFailed",
    "CircuitBreaker",
    "Completion",
    "LLMError",
    "Provider",
    "breaker",
    "complete",
    "configured_providers",
]
