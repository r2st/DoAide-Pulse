"""SEO hygiene applied to generated content — deterministic, not another LLM call.

The model is asked for a meta description and keywords as part of the generation
envelope, but what it returns needs enforcing: descriptions run long, keywords
come back as a wall of near-duplicates, and headers get skipped so a post opens
at H3. All of that is mechanical, so it is done here rather than spent as a
second round-trip.

:func:`audit` is the same rules read out loud, for the editor's SEO panel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Google truncates meta descriptions around 155-160 characters. We aim under
#: the lower bound so a trailing word is never clipped mid-thought.
META_DESCRIPTION_MAX = 155
META_DESCRIPTION_MIN = 70

#: Titles beyond ~60 chars get an ellipsis in results pages.
TITLE_MAX = 60

#: More than this and the keywords stop being a focus and start being a list.
KEYWORD_MAX = 8

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_MARKUP = re.compile(r"[*_`>\[\]()#]")


def strip_markdown(markdown: str) -> str:
    """Body text with fences, markup and link syntax removed.

    Used for excerpting and word counts; not a general-purpose renderer.
    """
    text = _CODE_FENCE.sub(" ", markdown)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> label
    text = _INLINE_MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_at_sentence(text: str, limit: int) -> str:
    """Trim *text* to *limit* chars, preferring a sentence boundary.

    Falls back to a word boundary with an ellipsis, so the result never ends
    mid-word — a clipped meta description reads as broken, not as truncated.
    """
    text = text.strip()
    if len(text) <= limit:
        return text

    kept: list[str] = []
    length = 0
    for sentence in _SENTENCE_END.split(text):
        if length + len(sentence) + 1 > limit:
            break
        kept.append(sentence)
        length += len(sentence) + 1
    if kept:
        return " ".join(kept).strip()

    clipped = text[: limit - 1]
    return clipped[: clipped.rfind(" ")].rstrip(",;:") + "…"


def build_meta_description(candidate: str, *, fallback_body: str) -> str:
    """A meta description of the right length, from the model's or the body."""
    text = strip_markdown(candidate).strip() or strip_markdown(fallback_body)
    return truncate_at_sentence(text, META_DESCRIPTION_MAX)


def normalize_keywords(keywords: list[str], *, extra: list[str] | None = None) -> list[str]:
    """Lowercase, dedupe, drop noise, and cap at :data:`KEYWORD_MAX`.

    Order is preserved because the model's ordering is roughly its own
    relevance ranking, and the first keyword is the one that ends up in the
    title check.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in [*keywords, *(extra or [])]:
        keyword = re.sub(r"\s+", " ", str(raw)).strip().lower().strip("#,.")
        # Single characters and bare numbers are never a useful keyword.
        if len(keyword) < 2 or keyword.isdigit() or keyword in seen:
            continue
        seen.add(keyword)
        out.append(keyword)
    return out[:KEYWORD_MAX]


def build_excerpt(body_markdown: str, *, limit: int = 220) -> str:
    """The first real paragraph, trimmed — used as the social blurb."""
    for block in body_markdown.split("\n\n"):
        text = strip_markdown(block)
        # Skip the title line and any leading image/badge row.
        if len(text) > 40 and not block.lstrip().startswith("#"):
            return truncate_at_sentence(text, limit)
    return truncate_at_sentence(strip_markdown(body_markdown), limit)


@dataclass(frozen=True)
class SeoIssue:
    """One thing to fix, and how bad it is."""

    #: "warn" is advisory; "error" means a platform or a crawler will visibly
    #: mishandle the post.
    level: str
    field: str
    message: str


def audit(
    *,
    title: str,
    body_markdown: str,
    meta_description: str,
    keywords: list[str],
) -> list[SeoIssue]:
    """Everything wrong with this piece's SEO, worst first.

    Returns an empty list for a clean piece. The editor shows this verbatim, so
    every message says what to do, not just what is wrong.
    """
    issues: list[SeoIssue] = []

    if not title.strip():
        issues.append(SeoIssue("error", "title", "The post has no title."))
    elif len(title) > TITLE_MAX:
        issues.append(
            SeoIssue(
                "warn",
                "title",
                f"Title is {len(title)} characters — search results cut off around "
                f"{TITLE_MAX}. Trim the tail.",
            )
        )

    if not meta_description.strip():
        issues.append(
            SeoIssue("error", "meta_description", "No meta description — search "
                     "engines will invent one from the body.")
        )
    elif len(meta_description) < META_DESCRIPTION_MIN:
        issues.append(
            SeoIssue(
                "warn",
                "meta_description",
                f"Meta description is only {len(meta_description)} characters. "
                f"Aim for {META_DESCRIPTION_MIN}–{META_DESCRIPTION_MAX}.",
            )
        )

    if not keywords:
        issues.append(
            SeoIssue("warn", "keywords", "No keywords set — nothing to optimise for.")
        )
    elif keywords and title:
        primary = keywords[0].lower()
        if primary not in title.lower():
            issues.append(
                SeoIssue(
                    "warn",
                    "title",
                    f'Primary keyword "{keywords[0]}" does not appear in the title.',
                )
            )

    headings = _HEADING.findall(body_markdown)
    levels = [len(hashes) for hashes, _ in headings]
    if not levels:
        issues.append(
            SeoIssue(
                "warn",
                "body",
                "No headings — long posts without H2s are hard to scan and rank.",
            )
        )
    else:
        # A jump from H2 straight to H4 breaks the document outline. Only the
        # *first* such jump is reported: after one, the rest are noise.
        previous = levels[0]
        for level in levels[1:]:
            if level > previous + 1:
                issues.append(
                    SeoIssue(
                        "warn",
                        "body",
                        f"Heading levels skip from H{previous} to H{level}. "
                        "Keep the outline contiguous.",
                    )
                )
                break
            previous = level

    words = len(strip_markdown(body_markdown).split())
    if words < 300:
        issues.append(
            SeoIssue(
                "warn",
                "body",
                f"Only {words} words. Posts under 300 rarely rank for anything.",
            )
        )

    # Errors first so the editor's panel leads with what actually breaks.
    return sorted(issues, key=lambda i: 0 if i.level == "error" else 1)


__all__ = [
    "KEYWORD_MAX",
    "META_DESCRIPTION_MAX",
    "META_DESCRIPTION_MIN",
    "TITLE_MAX",
    "SeoIssue",
    "audit",
    "build_excerpt",
    "build_meta_description",
    "normalize_keywords",
    "strip_markdown",
    "truncate_at_sentence",
]
