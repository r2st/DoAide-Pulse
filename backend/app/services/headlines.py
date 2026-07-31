"""Headline variants, applying a change, and telling which one worked.

Herald doesn't split live traffic — a piece publishes to a handful of
platforms, not to two random halves of an audience — so this is not
statistical A/B testing. What it is: generate alternative headlines, let a
human swap the live one, and remember exactly when each title was live. The
engagement snapshots in ``app.models.metrics.ContentMetric`` are already an
append-only time series, so once the swap times are recorded, attributing a
metric to whichever headline was live when it was captured is just a
timestamp comparison — no new tracking infrastructure needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Publication
from app.services import ai

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write headlines for developer-marketing content. You never invent "
    "features, numbers or claims beyond what you are given. You always reply "
    "with a single JSON object and nothing else."
)

#: Free reasoning models burn a near-fixed scratchpad allowance regardless of
#: how short the requested output is — see the constant of the same name in
#: app.services.content_generator, where the number was measured.
_REASONING_ALLOWANCE_TOKENS = 5000
_OUTPUT_TOKENS = 400

#: Generic transforms of the existing title. Nobody ships one of these
#: unedited — the point is a fallback that is never empty, the same tier
#: app.services.content_generator falls back to when every provider is down.
_FALLBACK_TEMPLATES = [
    "{title}: What You Need to Know",
    "Why {title}",
    "{title} — A Closer Look",
    "The Case for {title}",
    "{title}, Explained",
    "A Practical Look at {title}",
]


@dataclass
class HeadlineVariants:
    variants: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    is_fallback: bool = False


def _fallback_variants(content: Content, count: int) -> list[str]:
    out: list[str] = []
    for template in _FALLBACK_TEMPLATES[:count]:
        candidate = template.format(title=content.title).strip()[:300]
        if candidate and candidate != content.title:
            out.append(candidate)
    return out


def generate_variants(
    content: Content, project: Project, *, count: int = 4
) -> HeadlineVariants:
    """Draft alternative headlines for one piece. Never raises."""
    facts = [f"Current title: {content.title}"]
    if content.focus_keyword:
        facts.append(f"Primary SEO keyword: {content.focus_keyword}")
    if content.excerpt:
        facts.append(f"What it's about: {content.excerpt}")
    if project.name:
        facts.append(f"Project: {project.name}")

    prompt = f"""Suggest {count} alternative headlines for this piece of content.

{chr(10).join(facts)}

Rules:
- Each headline under 70 characters.
- Vary the style across the set (a question, a number/list form, a how-to
  form, a direct claim) rather than rewording the same sentence {count} times.
- Only claim what the facts above already imply — no invented feature,
  statistic or comparison.
- None of them may be identical to the current title.

Reply with exactly this JSON object and nothing else:
{{"variants": ["headline 1", "headline 2", "..."]}}"""

    try:
        payload, completion = ai.json_completion(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=_OUTPUT_TOKENS + _REASONING_ALLOWANCE_TOKENS,
        )
    except ai.AIError as exc:
        logger.info("headline generation for content %s fell back: %s", content.id, exc)
        return HeadlineVariants(variants=_fallback_variants(content, count), is_fallback=True)

    seen = {content.title.strip().lower()}
    variants: list[str] = []
    for candidate in ai.as_str_list(payload.get("variants"), limit=count):
        cleaned = candidate.strip()[:300]
        if not cleaned or ai.looks_like_reasoning(cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append(cleaned)

    if not variants:
        logger.warning(
            "headline generation for content %s returned nothing usable — using templates",
            content.id,
        )
        return HeadlineVariants(variants=_fallback_variants(content, count), is_fallback=True)

    return HeadlineVariants(
        variants=variants, provider=completion.provider, model=completion.model
    )


# --------------------------------------------------------------------------- #
# Applying a headline, and measuring the windows                              #
# --------------------------------------------------------------------------- #


def apply_headline(content: Content, new_title: str, *, now: datetime | None = None) -> None:
    """Swap the live title, closing out the previous one's window.

    Mutates *content* in place; the caller commits. Deliberately allowed on
    already-published content — that is the point of headline testing — and
    the slug is left untouched, since changing it there would break the URL
    the piece is already live at.
    """
    moment = (now or utcnow()).isoformat()
    history = list(content.headline_history or [])
    window_start = history[-1]["ended_at"] if history else content.created_at.isoformat()
    history.append({"title": content.title, "started_at": window_start, "ended_at": moment})
    # Reassign rather than mutate in place: the JSON column has no change
    # tracking for in-place list mutation, so an .append() here would be
    # silently lost on commit.
    content.headline_history = history
    content.title = new_title.strip()


@dataclass
class HeadlineWindow:
    """One headline, the window it was live, and what happened during it."""

    title: str
    started_at: datetime
    ended_at: datetime | None
    current: bool
    views: int = 0
    engagement: int = 0
    snapshots: int = 0


def performance(content: Content, db: Session) -> list[HeadlineWindow]:
    """Engagement recorded while each headline (including the current one) was live.

    Chronological order — oldest headline first, current one last and open-
    ended. Every metric snapshot for this content's publications falls into
    exactly one window, since the windows are contiguous and derived from the
    same swap timestamps that close and open them.
    """
    history = list(content.headline_history or [])
    windows = [
        HeadlineWindow(
            title=entry["title"],
            started_at=datetime.fromisoformat(entry["started_at"]),
            ended_at=datetime.fromisoformat(entry["ended_at"]),
            current=False,
        )
        for entry in history
    ]
    current_start = (
        datetime.fromisoformat(history[-1]["ended_at"]) if history else content.created_at
    )
    windows.append(
        HeadlineWindow(title=content.title, started_at=current_start, ended_at=None, current=True)
    )

    metrics = db.scalars(
        select(ContentMetric)
        .join(Publication, Publication.id == ContentMetric.publication_id)
        .where(Publication.content_id == content.id)
        .order_by(ContentMetric.captured_at)
    )
    for metric in metrics:
        captured = as_aware(metric.captured_at)
        for window in windows:
            start = as_aware(window.started_at)
            end = as_aware(window.ended_at) if window.ended_at else None
            if captured >= start and (end is None or captured < end):
                window.views += metric.views or 0
                window.engagement += metric.engagement
                window.snapshots += 1
                break

    return windows


__all__ = [
    "HeadlineVariants",
    "HeadlineWindow",
    "apply_headline",
    "generate_variants",
    "performance",
]
