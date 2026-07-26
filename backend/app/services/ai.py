"""The single entry point every AI feature calls, plus the text-hygiene helpers.

:func:`chat_completion` delegates to :mod:`app.services.llm_router`, which tries
OpenRouter, Gemini, Groq and Cerebras in order before giving up. Callers catch
:class:`AIError` and fall back to a deterministic template.

We only ever use free-tier models (never Anthropic directly). Everything is
plain httpx underneath so it stays trivially mockable in tests — no SDK, no
hidden global state.

One provider quirk shapes this module. The free tier's reasoning models
(``openai/gpt-oss-20b:free`` in particular) frequently return ``content: null``
with the whole response — chain-of-thought *and* the answer — parked in
``reasoning``. Two consequences:

* the router falls back to ``reasoning`` rather than crashing on ``None``;
* callers that put the text in front of a reader must run it through
  :func:`looks_like_reasoning` first, or a published blog post opens with
  "We need to write an announcement. The user wants…".

Asking for JSON sidesteps the whole problem: the object is still in there, and
:func:`extract_json_object` finds it. That is why every generator prompt in
Herald asks for a JSON envelope rather than raw prose.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services import llm_router


class AIError(RuntimeError):
    """Raised when no provider in the chain could produce a completion."""


def _balanced_objects(text: str):
    """Yield every balanced ``{...}`` span in *text*, largest-first per start.

    A brace-counting scan rather than ``find("{")`` to ``rfind("}")``. That
    naive span is wrong in both directions against real free-tier output: a
    reasoning scratchpad contains several partial objects, so the span runs from
    the first one's opening brace to the last one's closing brace and parses as
    nothing; and a *truncated* response has no final brace at all, so the span
    ends on a brace inside the post body.

    String literals are tracked so a ``{`` inside ``body_markdown`` — a code
    snippet, an f-string, a CSS rule — does not throw off the depth count.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        # A stray `}` with depth 0 is scratchpad noise, not the end of anything.
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                yield text[start : index + 1]
                start = -1


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the most substantial JSON object out of a model response.

    Tolerates markdown fences and surrounding prose — including a reasoning
    model's scratchpad, which is why asking for JSON is the reliable way to get
    clean reader-facing text out of the free tier.

    When a scratchpad contains several candidate objects (a sketch, then the
    real answer), the *largest* one wins. That is reliably the finished article
    rather than a plan of it.
    """
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)

    best: dict[str, Any] | None = None
    for candidate in _balanced_objects(text):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and (best is None or len(candidate) > _size(best)):
            best = parsed
    return best


def _size(payload: dict[str, Any]) -> int:
    """Serialized length of a parsed object, for comparing candidates."""
    try:
        return len(json.dumps(payload))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


# Openers and connectives that only ever show up in a model thinking out loud.
_REASONING_MARKERS = (
    "we need to",
    "the user says",
    "the user wants",
    "the user asks",
    "the instruction",
    "the developer",
    "let's produce",
    "let's write",
    "let me write",
    "so we need",
    "we must",
    "we should produce",
    "okay, so",
    "first, i",
)


def looks_like_reasoning(text: str) -> bool:
    """True when *text* reads as chain-of-thought rather than a finished answer.

    Deliberately conservative — it only fires on the opening of the text and on
    repeated first-person-planning markers, so a legitimate post that happens to
    contain "we must" once is not thrown away.
    """
    if not text:
        return False
    head = re.sub(r"\s+", " ", text.strip().lower())[:300]
    if head.startswith(_REASONING_MARKERS):
        return True
    return sum(1 for marker in _REASONING_MARKERS if marker in head) >= 2


def chat_completion_detailed(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> llm_router.Completion:
    """Run the provider chain and return the text *plus* who served it.

    Use this over :func:`chat_completion` when the caller wants to record which
    model produced a piece of text — content rows do, so the analytics page can
    answer "does the 120b model actually write better posts?".
    """
    try:
        return llm_router.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except llm_router.AllProvidersFailed as exc:
        raise AIError(str(exc)) from exc


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> str:
    """Get a completion from the first provider in the chain that works.

    Raises :class:`AIError` if no provider is configured or all of them fail —
    the caller's cue to fall back to its own static template.
    """
    return chat_completion_detailed(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    ).text


def json_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> tuple[dict[str, Any], llm_router.Completion]:
    """Ask for a JSON object and return it alongside the raw completion.

    Raises :class:`AIError` when the chain fails *or* when what came back has no
    JSON object in it. Both are the same thing to a caller: no usable output.
    """
    completion = chat_completion_detailed(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    parsed = extract_json_object(completion.text)
    if parsed is None:
        raise AIError(
            f"{completion.provider} returned no parseable JSON object "
            f"({completion.text[:120]!r}…)"
        )
    return parsed, completion


def as_str(value: Any, default: str = "") -> str:
    """Coerce one field of a model's JSON to a stripped string.

    Free models are loose about types — a field documented as a string comes
    back as a number, or as ``["a", "b"]``, often enough to be worth handling
    rather than raising.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(as_str(v) for v in value).strip()
    return str(value).strip()


def as_str_list(value: Any, *, limit: int | None = None) -> list[str]:
    """Coerce one field of a model's JSON to a list of non-empty strings.

    Accepts a real list, or the comma-separated string models substitute for
    one about a third of the time. Deduplicates case-insensitively while
    preserving the model's ordering, which is roughly its own ranking.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = as_str(item)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out[:limit] if limit else out


def as_float(value: Any, default: float, *, low: float = 0.0, high: float = 1.0) -> float:
    """Coerce and clamp a numeric field, falling back to *default*."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
