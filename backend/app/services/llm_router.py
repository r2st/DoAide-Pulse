"""Multi-provider LLM chain — OpenRouter → Gemini → Groq → Cerebras → caller's template.

Every AI feature in Herald (post drafting, headline generation, SEO metadata,
per-platform blurbs) funnels through :func:`complete`. The point is that one
flaky upstream must never turn into a silent product failure: a draft either
gets written by *some* model, or the caller falls back to a deterministic
template — but it is never empty and never a stack trace.

Four things make that work:

* **One dialect.** All four providers speak OpenAI's ``/chat/completions``, so
  they differ only in base URL, key and model name. A provider with no API key
  configured is skipped rather than attempted-and-failed.
* **A second sweep, but only when a sweep would help.** A 429 is the upstream
  saying *come back*; a 400 or a 401 is it saying *never*. When — and only
  when — an entire pass over the chain failed on rate limits alone, the chain
  waits and sweeps again. Waiting is deferred until every provider has been
  asked so that a working one is never delayed by a rate-limited one ahead of
  it. See :func:`_pause_before_retrying`.
* **A circuit breaker.** After ``llm_breaker_threshold`` consecutive failures a
  provider is skipped for ``llm_breaker_cooldown_seconds``. Without it a dead
  upstream costs a full timeout on *every* request, and the chain's latency is
  the sum of everything broken ahead of the one that works. The first success
  resets the count.
* **A terminal template tier.** When every provider is exhausted the chain
  raises :class:`AllProvidersFailed` rather than returning generic filler. Each
  call site owns a *specific* fallback that beats anything this module could
  invent, and the chain's job is to say "you're on your own now".

**Rate limits are the failure mode this module is really for.** The free tiers
Herald runs on meter two different things, and the two want opposite responses:

* a *per-minute* limit clears in seconds, so the right answer is to wait the few
  seconds and ask again. Falling straight through to the next provider — and
  eventually to a confidence-0 template that can never auto-publish — throws
  away a request that would have succeeded on the second attempt;
* a *per-day* quota does not clear at all today, so the right answer is to stop
  asking. A provider whose 429 carries a long ``Retry-After`` opens its breaker
  for exactly that long (capped at ``llm_breaker_max_cooldown_seconds``) rather
  than for the default five minutes, which would otherwise mean paying one
  refused request every five minutes until midnight.

Telling them apart is what ``Retry-After`` is for, and the free tiers do send
it. When they do not, :data:`_DEFAULT_RATE_LIMIT_PAUSE` assumes the per-minute
case, because that is both the common one and the one where guessing wrong is
cheap.

The last resort inside a single provider is a **different model on the same
key**. Free-tier quotas are metered per model, so a key that has spent its
budget on ``gpt-oss-20b:free`` still has one for ``gpt-oss-120b:free``; trying
the sibling costs one request and succeeds far more often than moving to a
provider whose key may not be configured at all. See :func:`_model_chain`.

The breaker state is per-process, in-memory. With several workers that means
each learns about a dead provider independently — fine, since the cost of
learning is one timeout and the alternative (shared state in Redis) buys little
for how rarely this fires.
"""
from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: How long to wait out a rate limit that arrived without a ``Retry-After``.
#: Assumes the per-minute case — see the module docstring.
_DEFAULT_RATE_LIMIT_PAUSE = 20.0

#: Statuses that mean "come back later" rather than "this request is wrong".
#: 408 and 409 are in for completeness; 429 and the 5xx family are what the free
#: tiers actually send.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """A single provider call failed.

    ``retryable`` is whether asking this provider this question again, *without
    moving on first*, could plausibly work. True only when the provider is up
    and answering and has effectively said "later": a 429, or a 5xx. False for a
    malformed request or a rejected key, where a replay spends the budget twice
    for the same answer — and false for a transport failure, where the provider
    chain itself is the better retry.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMRateLimited(LLMError):
    """The provider refused the request and may have said when to come back.

    ``retry_after`` is the provider's own answer in seconds, or ``None`` when it
    declined to give one. It drives both the in-process backoff and — when it is
    long enough to mean a spent daily quota — how long the breaker stays open.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class LLMModelUnavailable(LLMError):
    """The provider is up, the key is good, and the *model* does not exist.

    A retired model name is the one provider failure that cannot come back on
    its own. Everything else here is a wait: a rate limit ends, a 5xx passes, a
    transport error was the network. This one is a value in ``.env`` that has to
    change, and until somebody changes it the provider is dead.

    It was worth separating because the two are indistinguishable in a log until
    you read the body. Herald's Gemini slot answered
    ``404 … models/gemini-2.0-flash is no longer available`` on every generation
    from at least 2026-08-11, at ``WARNING``, in the middle of the ordinary
    per-minute rate-limit chatter from the other free tiers — so a provider that
    had been contributing nothing for days looked exactly like one having a bad
    afternoon. Distinguishing it buys two things:

    * the log line says what to *do*, and says it at ``ERROR``;
    * the breaker holds the provider down for a long cooldown instead of
      re-asking every sweep forever, which is the only honest response to a
      failure that has no chance of resolving.
    """


#: Fragments that mark a 4xx as naming a model rather than the request. Matched
#: case-insensitively against the response body. Deliberately narrow: a false
#: positive parks a *working* provider for the long cooldown, which is a worse
#: outcome than the noise this replaces.
_MODEL_GONE_MARKERS = (
    "is no longer available",
    "model not found",
    "model_not_found",
    "does not exist",
    "unknown model",
    "invalid model",
    "no longer supported",
    "has been deprecated",
    "is not a valid model",
)


def _looks_like_a_missing_model(body: str) -> bool:
    """Whether a 4xx body is about the model name rather than the request."""
    lowered = body.lower()
    return any(marker in lowered for marker in _MODEL_GONE_MARKERS)


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible chat endpoint."""

    name: str
    api_key: str
    base_url: str
    model: str
    # OpenRouter wants attribution headers; nobody else needs extras.
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Other models on this same key, tried in order once the primary is
    #: rate-limited or refuses. See :func:`_model_chain`.
    fallback_models: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        """The chat-completions endpoint. Every provider here speaks that shape."""
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> dict[str, str]:
        """Auth and content-type, plus whatever this provider needs on top.

        A method rather than a property because it builds a fresh dict: a shared
        one handed to a caller that mutated it would change every later request.
        """
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }


def _configured_models(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated ``*_FALLBACK_MODELS`` setting."""
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


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
            fallback_models=_configured_models(settings.openrouter_fallback_models),
        ),
        Provider(
            name="gemini",
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
            fallback_models=_configured_models(settings.gemini_fallback_models),
        ),
        Provider(
            name="groq",
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            model=settings.groq_model,
            fallback_models=_configured_models(settings.groq_fallback_models),
        ),
        Provider(
            name="cerebras",
            api_key=settings.cerebras_api_key,
            base_url=settings.cerebras_base_url,
            model=settings.cerebras_model,
            fallback_models=_configured_models(settings.cerebras_fallback_models),
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

    def open_for(self, name: str, seconds: float, *, now: float | None = None) -> None:
        """Skip *name* for *seconds*, whatever its failure count.

        For the one case the consecutive-failure counter reads wrong: a provider
        that answered 429 with a long ``Retry-After`` has told us its quota is
        spent, and there is nothing to learn from the two further failures the
        threshold would otherwise wait for. Never shortens a window already
        open — a later, vaguer refusal must not undo a definite one.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.open_until = max(state.open_until, now + max(0.0, seconds))
            state.failures = 0

    def record_success(self, name: str) -> None:
        """Clear *name*'s failure count and any open circuit.

        Discards the state rather than decrementing it. A provider that answered
        is working now, and a half-remembered run of failures from an outage an
        hour ago would trip the breaker early on the next unrelated blip.
        """
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


#: Phrases a provider uses when the refusal is a quota rather than a fault.
#: Matched against the *message* because the compat layers that answer 200 with
#: an error body frequently omit the numeric code as well.
_RATE_LIMIT_PHRASES = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "quota",
    "too many requests",
    "resource_exhausted",
    "resource exhausted",
)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait, from a ``Retry-After`` header. ``None`` if unusable.

    Two shapes are legal (RFC 9110 §10.2.3) and both are in the wild: a delay in
    seconds, and an HTTP-date. A date already in the past reads as "no
    guidance" rather than as a negative wait.
    """
    raw = ""
    try:
        raw = (headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    except AttributeError:  # pragma: no cover - a mapping-less test double
        return None
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:  # pragma: no cover - parsedate_to_datetime raises instead
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _looks_rate_limited(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)


def _status_error(provider: Provider, resp: httpx.Response) -> LLMError:
    """The error a non-2xx deserves.

    A 401/403 is a key problem and a 400 is a request problem; neither improves
    on a second attempt, and retrying a bad key across four providers is how one
    misconfiguration becomes sixteen requests.
    """
    body = _short(getattr(resp, "text", "") or "")
    status = resp.status_code

    if status == 429 or (status in _RETRYABLE_STATUSES and _looks_rate_limited(body)):
        return LLMRateLimited(
            f"{provider.name} rate-limited the request ({status}): {body}",
            retry_after=_retry_after(resp.headers),
        )
    # Checked only for a non-retryable 4xx, and only on the body: a 5xx that
    # happens to contain the words is the provider being broken, not the model
    # being gone, and standing it down for the long cooldown would be wrong.
    if status not in _RETRYABLE_STATUSES and _looks_like_a_missing_model(body):
        return LLMModelUnavailable(
            f"{provider.name} does not have the model it is configured with "
            f"({status}): {body}"
        )
    return LLMError(
        f"{provider.name} returned {status}: {body}",
        retryable=status in _RETRYABLE_STATUSES,
    )


def _body_error(provider: Provider, data: dict) -> LLMError:
    """The error a 200-with-no-choices deserves.

    OpenRouter's free tier answers a spent quota with exactly this: HTTP 200,
    no ``choices``, and ``error.message`` reading "Rate limit exceeded:
    free-models-per-day". Read as a plain failure it costs the chain a provider
    it could have come back to; read as a rate limit it opens the breaker for as
    long as the provider asked for and stops the next fifty requests bothering.
    """
    error = data.get("error")
    error = error if isinstance(error, dict) else {}
    detail = str(error.get("message") or "no choices returned")
    code = error.get("code")

    metadata = error.get("metadata")
    headers = metadata.get("headers") if isinstance(metadata, dict) else None
    retry_after = _retry_after(headers) if isinstance(headers, dict) else None

    if code in (429, "429", "rate_limit_exceeded") or _looks_rate_limited(detail):
        return LLMRateLimited(
            f"{provider.name} rate-limited the request: {detail}",
            retry_after=retry_after,
        )
    # The same configuration fault can arrive this way: these compat layers
    # report a retired model name as a 200 with an error body as readily as they
    # report a spent quota. See :class:`LLMModelUnavailable`.
    if _looks_like_a_missing_model(detail):
        return LLMModelUnavailable(
            f"{provider.name} does not have the model it is configured with: {detail}"
        )
    return LLMError(f"{provider.name} request failed: {detail}")


def _short(text: str, limit: int = 200) -> str:
    """A response body trimmed to something that belongs in a log line."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _sleep(seconds: float) -> None:
    """Indirection so tests can exercise the backoff without waiting for it."""
    time.sleep(seconds)


def _model_chain(provider: Provider, override: str | None,
                 extra: tuple[str, ...] = ()) -> list[str]:
    """Which models to try on *provider*, in order.

    The primary is the caller's override where it applies, otherwise the
    provider's configured model. Anything after it is a second bite at the same
    key — see the module docstring on why that beats moving providers.

    An explicit per-call override only makes sense for the provider it was
    written for; everyone else gets their own configured model. Same for the
    caller's *extra* fallbacks.
    """
    own = provider.name == "openrouter"
    primary = override if (override and own) else provider.model

    chain: list[str] = []
    for candidate in (primary, *(extra if own else ()), *provider.fallback_models):
        if candidate and candidate not in chain:
            chain.append(candidate)
    return chain


def _call(
    provider: Provider,
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """One provider attempt with one model. Raises :class:`LLMError` on failure.

    *model* is already resolved — :func:`_model_chain` decides which of a
    provider's models this attempt is for, so this function never has to know
    whose override it is holding.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        resp = httpx.post(
            provider.url, json=payload, headers=provider.headers(), timeout=timeout
        )
    except httpx.HTTPError as exc:
        # Not retried here, deliberately. A provider that cannot be reached at
        # all has three more behind it, and the chain gets to a working one
        # faster by moving on than by sleeping in front of a dead socket. The
        # in-process retry exists for the opposite case — a provider that is up
        # and answering, and has said to come back in a moment.
        raise LLMError(f"{provider.name} request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise _status_error(provider, resp)

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError(
            f"{provider.name} returned a body that is not JSON: {exc}"
        ) from exc

    # OpenRouter (and Gemini's compat layer) report upstream errors as a 200
    # with an `error` body rather than a non-2xx status. A rate limit arrives
    # this way far more often than as a real 429, so the body has to be read as
    # carefully as the status line — see :func:`_body_error`.
    if isinstance(data, dict) and "choices" not in data:
        raise _body_error(provider, data)

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


def _note_failure(provider: Provider, model: str, exc: LLMError) -> None:
    """Record one exhausted (provider, model) against the breaker.

    A rate limit that named a window *longer than this request is willing to
    wait for* is treated as authoritative — a spent daily quota, in practice.
    The provider has already said in seconds when it will serve us again, so
    nothing is learned by asking twice more to satisfy the failure threshold,
    and the breaker is opened on that one answer.

    A *short* wait is deliberately not stood down for. It is the per-minute case
    the second sweep exists to ride out, and opening the breaker for it would
    make the next sweep skip the very provider that is about to come back.
    """
    # Before the rate-limit branch, because this one cannot be waited out — and
    # deliberately *without* touching the breaker. A missing model condemns the
    # model, not the provider: the same key may serve a perfectly good
    # ``*_FALLBACK_MODELS`` entry, and standing the provider down here would
    # skip the fallback that was configured for exactly this. The chain in
    # :func:`_one_pass` opens the breaker once every model on the provider has
    # answered this way.
    #
    # ERROR rather than WARNING because the failure of the old behaviour was
    # visibility: Herald's Gemini slot answered "no longer available" on every
    # generation for days, at WARNING, among the free tiers' ordinary per-minute
    # rate-limit chatter, and nothing about the line said it would never stop.
    if isinstance(exc, LLMModelUnavailable):
        logger.error(
            "llm provider %s does not have model %s — set %s_MODEL (or "
            "%s_FALLBACK_MODELS) in .env to a model it still serves: %s",
            provider.name,
            model,
            provider.name.upper(),
            provider.name.upper(),
            exc,
        )
        return

    if isinstance(exc, LLMRateLimited) and exc.retry_after is not None:
        cooldown = min(
            exc.retry_after, float(settings.llm_breaker_max_cooldown_seconds)
        )
        if exc.retry_after > float(settings.llm_retry_max_backoff_seconds):
            breaker.open_for(provider.name, cooldown)
            logger.warning(
                "llm provider %s rate-limited on %s for longer than this request "
                "will wait; skipping it for %.0fs: %s",
                provider.name,
                model,
                cooldown,
                exc,
            )
            return

    tripped = breaker.record_failure(provider.name)
    logger.warning(
        "llm provider %s failed on %s (%s)%s",
        provider.name,
        model,
        exc,
        " — circuit opened" if tripped else "",
    )


@dataclass
class _Pass:
    """What one sweep of the whole chain produced, when it produced no text."""

    errors: list[str] = field(default_factory=list)
    #: Waits named by the providers that refused. Empty when nobody was
    #: rate-limited, which is the signal that waiting would achieve nothing.
    rate_limit_waits: list[float | None] = field(default_factory=list)
    #: True when at least one provider failed for a reason that is not a rate
    #: limit — a bad key, a 400, a dead host. Sleeping does not fix any of them.
    other_failure: bool = False


def _sweep(
    providers: list[Provider],
    messages: list[dict[str, str]],
    *,
    model: str | None,
    fallback_models: tuple[str, ...],
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> Completion | _Pass:
    """One pass over every provider and every model. No sleeping."""
    outcome = _Pass()

    for provider in providers:
        if breaker.is_open(provider.name):
            logger.debug("llm provider %s skipped (breaker open)", provider.name)
            outcome.errors.append(f"{provider.name}: skipped, circuit open")
            continue

        # Counted so a provider whose *every* model is gone can be stood down
        # below. Tracked here rather than in ``_note_failure`` because it is a
        # fact about the chain, not about any one call.
        tried = 0
        missing_models = 0
        for candidate in _model_chain(provider, model, fallback_models):
            tried += 1
            started = time.monotonic()
            try:
                text = _call(
                    provider,
                    messages,
                    model=candidate,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
            except LLMError as exc:
                _note_failure(provider, candidate, exc)
                outcome.errors.append(str(exc))
                if isinstance(exc, LLMRateLimited):
                    outcome.rate_limit_waits.append(exc.retry_after)
                else:
                    outcome.other_failure = True
                if isinstance(exc, LLMModelUnavailable):
                    missing_models += 1
                # A provider that has just named a cool-down has nothing more to
                # give this request, whichever model we ask for next.
                if breaker.is_open(provider.name):
                    break
                continue

            breaker.record_success(provider.name)
            logger.info(
                "llm served by %s (%s) in %.2fs",
                provider.name,
                candidate,
                time.monotonic() - started,
            )
            return Completion(text=text, provider=provider.name, model=candidate)

        # Every model this provider was given is gone. Now — and only now — it is
        # the provider that is misconfigured, and no amount of coming back
        # changes that, so it is held for the cooldown ceiling instead of being
        # re-asked on every sweep until somebody edits ``.env``.
        if tried and missing_models == tried and not breaker.is_open(provider.name):
            cooldown = float(settings.llm_breaker_max_cooldown_seconds)
            breaker.open_for(provider.name, cooldown)
            logger.error(
                "llm provider %s has no usable model configured (%d tried); "
                "skipping it for %.0fs",
                provider.name,
                tried,
                cooldown,
            )

    return outcome


def _pause_before_retrying(outcome: _Pass, providers: list[Provider]) -> float | None:
    """How long to wait before sweeping the chain again, or ``None``.

    ``None`` — do not sweep again — in three cases, and each is one where a nap
    changes nothing:

    * nobody was rate-limited, so the failures are not about timing;
    * something failed for a *different* reason as well, which the wait would
      not address and which the caller's template handles just as well now as in
      thirty seconds;
    * every remaining provider is behind an open breaker, so the next sweep
      would make no requests at all.

    Otherwise it is the shortest wait any refusing provider named — the first
    moment one of them will serve us — defaulting to
    :data:`_DEFAULT_RATE_LIMIT_PAUSE` where none said, and clamped so a worker
    is never held for longer than ``llm_retry_max_backoff_seconds``. Jittered
    for the reason :func:`_backoff_delay` gives.
    """
    if outcome.other_failure or not outcome.rate_limit_waits:
        return None
    if all(breaker.is_open(provider.name) for provider in providers):
        return None

    named = [wait for wait in outcome.rate_limit_waits if wait is not None]
    wait = min(named) if named else _DEFAULT_RATE_LIMIT_PAUSE
    wait = min(wait, float(settings.llm_retry_max_backoff_seconds))
    if wait <= 0:
        return None
    return random.uniform(wait / 2, wait)


def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    fallback_models: tuple[str, ...] | list[str] = (),
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> Completion:
    """Try each configured provider in order; return the first success.

    Within a provider, each model in its chain is tried before moving on —
    free-tier quota is per model, so the sibling is a real second chance.

    A sweep that fails *entirely* because of rate limits, and for no other
    reason, is slept on and repeated up to ``llm_max_attempts`` times. The
    ordering matters: nothing is slept on until every provider has been asked,
    so a working provider is never delayed by a rate-limited one ahead of it,
    and the wait is only ever paid in the situation it fixes — every free tier
    refusing at once, which otherwise means a confidence-0 template and an
    auto-publish that cannot happen.

    *fallback_models* lets a caller name siblings of its own *model* override;
    they apply to the same provider the override does.

    Raises :class:`AllProvidersFailed` when none of them produce usable text —
    the signal for the caller to use its own static template.
    """
    providers = _providers()
    if not providers:
        raise AllProvidersFailed(
            "No LLM provider is configured — set OPENROUTER_API_KEY, "
            "GEMINI_API_KEY, GROQ_API_KEY or CEREBRAS_API_KEY"
        )

    sweeps = max(1, int(settings.llm_max_attempts))
    errors: list[str] = []

    # `while True` rather than `range(1, sweeps + 1)`: the range bound and the
    # `sweep >= sweeps` guard below stated the same limit twice, and the guard
    # has to stay — reaching the bound by falling out of the loop would mean
    # pricing and sleeping off a pause after the last sweep, delaying the
    # caller's fallback by up to the rate-limit window for nothing.
    sweep = 0
    while True:
        sweep += 1
        outcome = _sweep(
            providers,
            messages,
            model=model,
            fallback_models=tuple(fallback_models),
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if isinstance(outcome, Completion):
            return outcome

        errors = outcome.errors
        if sweep >= sweeps:
            break
        pause = _pause_before_retrying(outcome, providers)
        if pause is None:
            break
        logger.info(
            "every llm provider is rate-limited; sweeping again in %.1fs "
            "(pass %d of %d)",
            pause,
            sweep,
            sweeps,
        )
        _sleep(pause)

    raise AllProvidersFailed("All LLM providers failed: " + "; ".join(errors))


__all__ = [
    "AllProvidersFailed",
    "CircuitBreaker",
    "Completion",
    "LLMError",
    "LLMModelUnavailable",
    "LLMRateLimited",
    "Provider",
    "breaker",
    "complete",
    "configured_providers",
]
