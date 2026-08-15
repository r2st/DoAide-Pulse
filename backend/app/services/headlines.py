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

from app.config import settings
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
    # focus_keyword and excerpt are nullable and routinely unset; the name is
    # not — the column is NOT NULL and every write path (create, patch, seed)
    # goes through a min_length=1 field, so there is no project to guard against.
    facts = [f"Current title: {content.title}"]
    if content.focus_keyword:
        facts.append(f"Primary SEO keyword: {content.focus_keyword}")
    if content.excerpt:
        facts.append(f"What it's about: {content.excerpt}")
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
            purpose="headlines",
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
    """One headline, the window it was live, and what it earned.

    ``views`` and ``engagement`` are what was *gained* during the window, not
    the running totals the snapshots carry. Platform counters are cumulative —
    Dev.to's ``page_views_count`` is lifetime views — so summing the snapshots
    that land in a window would count the same views once per poll and hand
    every contest to whichever headline happened to be live longest.
    """

    title: str
    started_at: datetime
    ended_at: datetime | None
    current: bool
    views: int = 0
    engagement: int = 0
    snapshots: int = 0

    def hours_live(self, *, now: datetime | None = None) -> float:
        """How long this headline was up. The open window runs to *now*."""
        end = as_aware(self.ended_at) if self.ended_at else (now or utcnow())
        return max((end - as_aware(self.started_at)).total_seconds(), 0.0) / 3600

    def views_per_day(self, *, now: datetime | None = None) -> float | None:
        """Views gained per day live, or ``None`` with nothing to divide by.

        This, rather than the raw count, is what makes two headlines
        comparable: one live for a fortnight will out-total one live for an
        afternoon whatever it said.
        """
        hours = self.hours_live(now=now)
        if hours <= 0 or self.snapshots == 0:
            return None
        return round(self.views / (hours / 24), 2)

    def as_dict(self, *, now: datetime | None = None) -> dict:
        """The wire shape of one headline's window.

        *now* is threaded through rather than read here so every window in a
        response is measured against the same instant — a list where each row
        used its own "now" is not internally comparable, which is the one thing
        these rows exist to be.
        """
        return {
            "title": self.title,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "current": self.current,
            "views": self.views,
            "engagement": self.engagement,
            "snapshots": self.snapshots,
            "hours_live": round(self.hours_live(now=now), 1),
            "views_per_day": self.views_per_day(now=now),
        }


def performance(content: Content, db: Session) -> list[HeadlineWindow]:
    """What each headline (including the current one) earned while it was live.

    Chronological order — oldest headline first, current one last and open-
    ended. Every metric snapshot for this content's publications falls into
    exactly one window, since the windows are contiguous and derived from the
    same swap timestamps that close and open them.

    Attribution is by *gain*, per publication: a snapshot's contribution is the
    difference from that publication's previous snapshot, credited to whichever
    headline was live when the later one was taken. A platform whose counter
    goes backwards (a purge, a rescrape) contributes zero rather than a
    negative, since no headline made views disappear.

    The running baseline is clamped to the *high-water mark*, not to the last
    reading, which is the same rule :mod:`app.services.velocity` applies to the
    same series. Storing the dip instead would credit the recovery as fresh
    gain on the next poll: a Bluesky post that ``getPosts`` briefly stops
    returning writes an all-``None`` snapshot whose ``engagement`` property
    coalesces to zero, and the poll after it would then hand the piece's entire
    lifetime engagement to whichever headline happened to be live — the same
    once-per-poll double count the append-only series exists to avoid.
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
        # Per publication, then in time order: the deltas below are only
        # meaningful against the same platform's previous reading.
        .order_by(ContentMetric.publication_id, ContentMetric.captured_at)
    )

    previous: dict[int, tuple[int, int]] = {}
    for metric in metrics:
        window = _window_for(windows, as_aware(metric.captured_at))
        if window is None:
            continue

        last_views, last_engagement = previous.get(metric.publication_id, (0, 0))
        views_now = max(
            last_views, metric.views if metric.views is not None else last_views
        )
        engagement_now = max(last_engagement, metric.engagement)
        previous[metric.publication_id] = (views_now, engagement_now)

        window.views += views_now - last_views
        window.engagement += engagement_now - last_engagement
        window.snapshots += 1

    return windows


def _window_for(
    windows: list[HeadlineWindow], captured: datetime
) -> HeadlineWindow | None:
    for window in windows:
        start = as_aware(window.started_at)
        end = as_aware(window.ended_at) if window.ended_at else None
        if captured >= start and (end is None or end > captured):
            return window
    return None


# --------------------------------------------------------------------------- #
# Picking a winner                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class Winner:
    """The verdict on a headline contest.

    ``confident`` is the only field an automated swap should read. Everything
    else exists so the UI — and the log line the swap writes — can say why.
    """

    #: The best-performing headline, which is often the one already live.
    title: str
    #: True when the best is not what is live *and* the evidence clears the
    #: bar. Nothing swaps a live headline on a maybe.
    confident: bool
    reason: str
    #: Views per day for the leader and for the headline currently live.
    score: float | None = None
    current_score: float | None = None
    #: Every window that had enough evidence to be considered, best first.
    ranked: list[HeadlineWindow] = field(default_factory=list)


def pick_winner(
    windows: list[HeadlineWindow], *, now: datetime | None = None
) -> Winner:
    """Which headline earned the most attention per day it was live.

    Three guards stand between a number and a swap, and each exists because of
    a way this can be wrong:

    * **Evidence.** A window needs ``HEADLINE_MIN_SNAPSHOTS`` polls and
      ``HEADLINE_MIN_WINDOW_HOURS`` of airtime before it is ranked at all. One
      poll an hour after a swap says nothing about a headline.
    * **Margin.** The challenger must beat the incumbent by
      ``HEADLINE_WINNER_MARGIN``. Without it, headlines flap forever on noise.
    * **The incumbent is judged too.** If the headline now live has not yet
      earned enough evidence, nothing is confident — which is also what stops a
      swap from immediately triggering another.

    One bias is worth stating plainly rather than pretending away: a post earns
    most of its views in its first days, so whichever headline was live at
    launch is flattered. The margin blunts it; it does not remove it. This is
    why the automated swap is opt-in per project.
    """
    moment = now or utcnow()
    ranked = sorted(
        (w for w in windows if _has_evidence(w, now=moment)),
        key=lambda w: (w.views_per_day(now=moment) or 0.0, w.engagement),
        reverse=True,
    )
    live = next((w for w in windows if w.current), None)
    live_score = live.views_per_day(now=moment) if live else None
    live_title = live.title if live else ""

    if not ranked:
        return Winner(
            title=live_title,
            confident=False,
            reason="Not enough data yet — no headline has been live long enough "
            "to judge.",
            current_score=live_score,
        )

    leader = ranked[0]
    leader_score = leader.views_per_day(now=moment) or 0.0

    if leader.current:
        return Winner(
            title=leader.title,
            confident=False,
            reason="The headline already live is the best performer.",
            score=leader_score,
            current_score=live_score,
            ranked=ranked,
        )

    if live is None or not _has_evidence(live, now=moment):
        return Winner(
            title=leader.title,
            confident=False,
            reason="The headline live now has not had a fair run yet — give it "
            "time before judging it against the others.",
            score=leader_score,
            current_score=live_score,
            ranked=ranked,
        )

    margin = settings.headline_winner_margin
    threshold = (live_score or 0.0) * (1 + margin)
    if leader_score <= threshold:
        return Winner(
            title=leader.title,
            confident=False,
            reason=(
                f"{leader.title!r} leads, but not by the "
                f"{round(margin * 100)}% needed to call it rather than noise."
            ),
            score=leader_score,
            current_score=live_score,
            ranked=ranked,
        )

    return Winner(
        title=leader.title,
        confident=True,
        reason=(
            f"{leader.title!r} earned {leader_score} views/day against "
            f"{live_score} for the headline live now, over {leader.snapshots} "
            "readings."
        ),
        score=leader_score,
        current_score=live_score,
        ranked=ranked,
    )


def _has_evidence(window: HeadlineWindow, *, now: datetime) -> bool:
    return (
        window.snapshots >= settings.headline_min_snapshots
        and window.hours_live(now=now) >= settings.headline_min_window_hours
        and window.views > 0
    )


def auto_select(
    content: Content, db: Session, *, now: datetime | None = None
) -> tuple[Winner, bool]:
    """Swap in the winning headline when the evidence is good enough.

    Returns ``(verdict, applied)``. Mutates *content* on a swap; the caller
    commits, in keeping with :func:`apply_headline`.
    """
    verdict = pick_winner(performance(content, db), now=now)
    if not verdict.confident or verdict.title == content.title:
        return verdict, False

    apply_headline(content, verdict.title, now=now)
    logger.info(
        "content %s headline auto-selected: %s", content.id, verdict.reason
    )
    return verdict, True


__all__ = [
    "HeadlineVariants",
    "HeadlineWindow",
    "Winner",
    "apply_headline",
    "auto_select",
    "generate_variants",
    "performance",
    "pick_winner",
]
