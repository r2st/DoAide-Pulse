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
import unicodedata
from collections.abc import Iterator
from typing import Any

from app.services import llm_router


class AIError(RuntimeError):
    """Raised when no provider in the chain could produce a completion."""


class UnusableResponse(AIError):
    """A provider answered, and what came back could not be used.

    A subclass, so every ``except AIError`` in the tree keeps catching it and
    keeps falling back to its own template — the *handling* is the same. What
    differs is what the caller may conclude afterwards, and the two had been
    indistinguishable because :func:`json_completion` raised the same class for
    both halves of "no usable output":

    * the chain failed — every key rate-limited, every circuit open, nothing
      asked, nothing answered. Transient, and it clears when the quota resets;
    * a provider answered and the reply held no JSON object. The commonest
      cause is a reply cut off at ``max_tokens`` with the envelope truncated
      mid-string, which is exactly what a generation failing *mid-article*
      looks like from here.

    Only the first is a reason to come back later. Treating the second as one
    put :mod:`app.tasks.autopilot_tasks` in a loop it could not leave: it holds
    the repo watermark on an outage, so the next scan read the same commits,
    built the same prompt, was answered the same way, and held the watermark
    again — every scan, forever, writing nothing and banking nothing while
    spending a GitHub read and a model call each time. See
    :data:`app.services.content_generator.FALLBACK_UNUSABLE`.
    """


def _balanced_objects(text: str) -> Iterator[str]:
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


#: Scripts that have no business appearing in the English copy Herald writes.
#:
#: Greek and the Latin supplements are deliberately *absent*. "a lambda folded
#: over sigma", "Bücher", and the typographic dashes, curly quotes and ellipses
#: every one of these models emits are all legitimate, and a gate that fired on
#: them would hold back good work for being correctly typeset.
#:
#: Written as escapes rather than glyphs on purpose: a range endpoint like
#: U+07BF is unassigned and renders as a box or as nothing at all, so the glyph
#: form of this table is unreadable in a diff and unreviewable in a review.
_FOREIGN_SCRIPTS = (
    "\u0400-\u07bf"  # Cyrillic, Armenian, Hebrew, Arabic, Syriac, Thaana
    "\u0900-\u11ff"  # Devanagari through Sinhala, Thai, Lao, Tibetan,
    #                    Myanmar, Georgian, Hangul Jamo
    "\u1200-\u137f"  # Ethiopic
    "\u1780-\u17ff"  # Khmer
    "\u3040-\u30ff"  # Hiragana, Katakana
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
)

_FOREIGN_RUN = re.compile(f"[{_FOREIGN_SCRIPTS}]+")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

#: Above this share of the letters, the text is *written* in another script
#: rather than contaminated by one, and none of this is the gate's business.
#: Real contamination is a rounding error by comparison — the runs seen in
#: production were five characters in an eight-hundred-word article.
_MULTILINGUAL_SHARE = 0.10


def stray_script_runs(text: str, *, limit: int = 10) -> list[str]:
    """Runs of foreign-script characters spliced into otherwise-English *text*.

    Free-tier models occasionally emit a token from another script in the middle
    of an English sentence — ``a repeatable scenario<MALAYALAM> framework``,
    ``caches agent availability and message<CJK>``, ``selects a publish time
    <CYRILLIC>``. The word is not a translation of anything nearby and carries no
    meaning; it is the sampler slipping. Nothing downstream noticed: the JSON
    parsed, the body was long enough, the SEO score was unaffected because the
    keyword density did not move, and the piece auto-published under the user's
    name with a Cyrillic noun wedged into paragraph three.

    Returned rather than repaired. Deleting the run is *usually* right and
    sometimes silently wrong — the model may have dropped the English word it
    meant to write, leaving "selects a publish time" as a sentence that now reads
    fine and means something else. So the runs are handed to a reviewer, who can
    see the sentence, and the piece is held back from auto-publishing rather than
    thrown away: it is one bad word in an otherwise good article.

    Text genuinely *written* in one of these scripts returns nothing — see
    :data:`_MULTILINGUAL_SHARE`. Herald writes English today, but a body that is
    thirty percent Devanagari is a translation, not a glitch, and this is the
    wrong gate to fail it at.

    *limit* caps the returned list; the count is what a reviewer acts on, not the
    hundredth run.
    """
    if not text:
        return []

    runs = _FOREIGN_RUN.findall(text)
    if not runs:
        return []

    foreign = sum(len(run) for run in runs)
    latin = len(_LATIN_LETTER.findall(text))
    if foreign >= (foreign + latin) * _MULTILINGUAL_SHARE:
        return []

    seen: list[str] = []
    for run in runs:
        if run not in seen:
            seen.append(run)
        if len(seen) >= limit:
            break
    return seen


#: Accented words English actually borrows. Everything else carrying a letter
#: from outside Basic Latin is treated as a splice.
#:
#: Short on purpose, and it does not need to be complete. A word missing from
#: here costs one glance at a review queue; a word wrongly *added* to it is a
#: hole in the gate for as long as it sits here.
_LOANWORDS = frozenset(
    {
        "résumé", "résumés", "resumé", "café", "cafés", "naïve", "naïveté",
        "cliché", "clichés", "façade", "façades", "fiancé", "fiancée",
        "déjà", "piñata", "jalapeño", "señor", "über", "doppelgänger",
        "crème", "brûlée", "entrée", "entrées", "soufflé", "purée", "sauté",
        "sautéed", "protégé", "protégée", "exposé", "décor", "début", "débuts",
        "éclair", "garçon", "voilà", "apéritif", "touché", "attaché",
        "communiqué", "née", "blasé", "smörgåsbord", "à",
    }
)

#: Greek, including the polytonic block. Split out from :data:`_FOREIGN_SCRIPTS`
#: because a lone Greek letter is prose a developer writes on purpose.
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")

#: A word: letters, plus the hyphens and apostrophes that hold a compound
#: together. Joining across the hyphen is what makes ``α-authentic`` one token
#: rather than a lone Greek letter standing next to an English word.
_WORD = re.compile(r"[^\W\d_]+(?:[-‐‑'’][^\W\d_]+)*")

_NON_ASCII_LETTER = re.compile(r"[^\x00-\x7f]")


def stray_letter_splices(text: str, *, limit: int = 10) -> list[str]:
    """English words carrying a letter that does not belong to them.

    The companion to :func:`stray_script_runs`, for the half of the same failure
    that one cannot see. That gate looks for *scripts* Herald never writes in,
    and it deliberately exempts Greek and the Latin supplements so that "a lambda
    folded over sigma", "Bücher" and "résumé" survive. The sampler slips inside
    those ranges too, and when it does the result reads as an ordinary word::

        the cost‑of‑capital methodology rënd the base scenario
        invoices are uploaded via a nətive file picker
        an SMB owner receives an alert in GoSumo's stärker dashboard
        démontrated latency improvements translate directly to …
        ensuring only α‑authentic requests modify the database

    Two of those published to Dev.to and Bluesky under the user's name and were
    not among the six the script gate later caught, because every character in
    them is one this module had good reason to allow.

    The discriminator is the *word*, not the character. ``é`` is fine in
    "résumé" and wrong in "démontrated", and no property of the codepoint
    separates them — so a word carrying a letter from outside Basic Latin is a
    splice unless it is one English genuinely borrows (:data:`_LOANWORDS`), or a
    single Greek letter standing on its own as a symbol.

    Returned whole rather than as the offending character, and returned rather
    than repaired, for the same reason as :func:`stray_script_runs`: "rënd" tells
    a reviewer where to look and what it was probably meant to say, where "ë"
    tells them almost nothing.

    Biased toward review, and the false positives are the honest cost: "Zürich"
    and "λ-calculus" are both held for a human glance. That is one click, against
    a corrupted post going out under the user's byline — which is what the
    unbiased version of this did six times, then twice more.
    """
    if not text:
        return []
    if not _NON_ASCII_LETTER.search(text):
        return []

    # Same escape hatch as the script gate: text genuinely written in another
    # language is not this gate's business either.
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return []
    foreign = sum(1 for ch in letters if ord(ch) > 127)
    if foreign >= len(letters) * _MULTILINGUAL_SHARE:
        return []

    seen: list[str] = []
    for word in _WORD.findall(text):
        odd = [ch for ch in word if ch.isalpha() and ord(ch) > 127]
        if not odd:
            continue
        # Runs of a script the other gate already names; reporting them twice
        # would make one bad word look like two problems.
        if _FOREIGN_RUN.search(word):
            continue
        if unicodedata.normalize("NFC", word).casefold() in _LOANWORDS:
            continue
        # A symbol, not a word: "the λ folds over σ".
        if len(odd) == 1 and len(word) == 1 and _GREEK.match(word):
            continue
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def chat_completion_detailed(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    fallback_models: tuple[str, ...] | list[str] = (),
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> llm_router.Completion:
    """Run the provider chain and return the text *plus* who served it.

    Use this over :func:`chat_completion` when the caller wants to record which
    model produced a piece of text — content rows do, so the analytics page can
    answer "does the 120b model actually write better posts?".

    *fallback_models* names siblings of *model* to try on the same provider
    before moving to the next one. Free-tier quotas are metered per model, so a
    caller that has a second acceptable model should say so — see
    :mod:`app.services.llm_router`.
    """
    try:
        return llm_router.complete(
            messages,
            model=model,
            fallback_models=fallback_models,
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
    fallback_models: tuple[str, ...] | list[str] = (),
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
        fallback_models=fallback_models,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    ).text


def json_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    fallback_models: tuple[str, ...] | list[str] = (),
    temperature: float = 0.7,
    max_tokens: int = 1200,
    timeout: float = 90.0,
) -> tuple[dict[str, Any], llm_router.Completion]:
    """Ask for a JSON object and return it alongside the raw completion.

    Raises when the chain fails *or* when what came back has no JSON object in
    it. Both are the same thing to a caller that only wants text — no usable
    output — and both are an :class:`AIError`, so a handler that does not care
    which is unchanged.

    They are *not* the same thing to a caller deciding whether to ask again,
    and the second is spelled :class:`UnusableResponse` for that caller's
    benefit. A provider that answered is a provider that is reachable; the
    reply being unreadable is a fact about this generation, not about the
    quota.
    """
    completion = chat_completion_detailed(
        messages,
        model=model,
        fallback_models=fallback_models,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    parsed = extract_json_object(completion.text)
    if parsed is None:
        raise UnusableResponse(
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
    """Coerce a numeric field to the ``[low, high]`` scale, or give up on it.

    A value *inside* the range is taken as written. A value outside it is not
    clamped to the nearest end — it is discarded for *default*, because a number
    off the scale is evidence the model was not answering on the scale at all,
    and both directions of clamping invent an answer nobody gave.

    The direction that matters is up. ``confidence`` gates unreviewed publishing
    (see :func:`app.services.content_generator._assemble`), and free models
    answer the 0.0–1.0 question on a 1–10 or 0–100 scale often enough to be
    worth handling: clamping turned ``"confidence": 3`` — a model saying *do not
    publish this* — into 1.0, the highest possible confidence, and sent it
    straight out. Falling back to *default* puts it in the review queue instead,
    which is the correct outcome for a piece whose self-assessment could not be
    read.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # NaN fails both comparisons and lands on the default, which is right: it is
    # the least readable answer of all.
    if not low <= number <= high:
        return default
    return number
