"""Posting cadence and timing suggestions for the calendar.

Deliberately a table of constants rather than a model call or a learned
schedule. Two reasons: the published guidance for these platforms is stable and
boring, and a suggestion the user can predict is one they can override with
confidence. When Herald has enough of the user's *own* metrics to beat the
table, that belongs in the analytics service — not here.

Everything is UTC. The calendar renders in the browser's timezone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.models.publication import Platform


@dataclass(frozen=True)
class Cadence:
    """Suggested rhythm for one platform."""

    platform: Platform
    #: Posts per week beyond which an audience starts tuning out.
    max_per_week: int
    #: Weekdays that historically perform best (0 = Monday).
    best_weekdays: tuple[int, ...]
    #: Best hour to post, UTC.
    best_hour_utc: int
    rationale: str


#: The table. Hours are UTC and chosen to land mid-morning US Eastern, which is
#: where the developer audience for all six platforms is densest.
CADENCES: dict[Platform, Cadence] = {
    Platform.DEVTO: Cadence(
        platform=Platform.DEVTO,
        max_per_week=3,
        best_weekdays=(1, 2, 3),  # Tue-Thu
        best_hour_utc=13,
        rationale="Dev.to's feed moves fast; Tue–Thu mornings ET get the longest "
        "run on the front page.",
    ),
    Platform.MEDIUM: Cadence(
        platform=Platform.MEDIUM,
        max_per_week=2,
        best_weekdays=(1, 3),  # Tue, Thu
        best_hour_utc=14,
        rationale="Medium's distribution is slower and curation-led — two "
        "considered posts beat five thin ones.",
    ),
    Platform.HASHNODE: Cadence(
        platform=Platform.HASHNODE,
        max_per_week=3,
        best_weekdays=(1, 2, 3),
        best_hour_utc=13,
        rationale="Similar audience and rhythm to Dev.to.",
    ),
    Platform.LINKEDIN: Cadence(
        platform=Platform.LINKEDIN,
        max_per_week=4,
        best_weekdays=(1, 2, 3),
        best_hour_utc=12,
        rationale="Weekday business hours. Weekend posts see a fraction of the "
        "reach.",
    ),
    Platform.TWITTER: Cadence(
        platform=Platform.TWITTER,
        max_per_week=10,
        best_weekdays=(0, 1, 2, 3, 4),
        best_hour_utc=15,
        rationale="The only platform here where daily is normal rather than "
        "excessive.",
    ),
    Platform.WORDPRESS: Cadence(
        platform=Platform.WORDPRESS,
        max_per_week=2,
        best_weekdays=(1, 3),
        best_hour_utc=13,
        rationale="Your own blog has no feed to time against — consistency "
        "matters more than the hour.",
    ),
}


def cadence_for(platform: Platform | str) -> Cadence:
    key = platform if isinstance(platform, Platform) else Platform(platform)
    return CADENCES[key]


def next_slot(
    platform: Platform | str, *, after: datetime, taken: list[datetime] | None = None
) -> datetime:
    """The next good time to post on *platform* after *after*.

    Skips any slot within 12 hours of one already in *taken*, so "schedule the
    next three" spreads them out instead of stacking them on the same morning.
    Searches four weeks ahead and then gives up and returns the last candidate —
    a calendar that silently returns nothing is worse than one that suggests a
    crowded slot.
    """
    cadence = cadence_for(platform)
    occupied = taken or []
    candidate = after

    for _ in range(28):
        candidate = (candidate + timedelta(days=1)).replace(
            hour=cadence.best_hour_utc, minute=0, second=0, microsecond=0
        )
        if candidate.weekday() not in cadence.best_weekdays:
            continue
        if any(abs((candidate - t).total_seconds()) < 12 * 3600 for t in occupied):
            continue
        return candidate

    return candidate


def suggest_schedule(
    platform: Platform | str, *, count: int, start: datetime
) -> list[datetime]:
    """*count* well-spaced slots for one platform, starting after *start*."""
    slots: list[datetime] = []
    cursor = start
    for _ in range(count):
        slot = next_slot(platform, after=cursor, taken=slots)
        slots.append(slot)
        cursor = slot
    return slots


def weekly_capacity(platforms: list[Platform | str]) -> int:
    """How many posts a week this set of platforms can absorb in total."""
    return sum(cadence_for(p).max_per_week for p in platforms)


def describe(platform: Platform | str) -> dict:
    """JSON-serializable cadence, for the calendar's sidebar."""
    cadence = cadence_for(platform)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        "platform": cadence.platform.value,
        "max_per_week": cadence.max_per_week,
        "best_weekdays": [days[d] for d in cadence.best_weekdays],
        "best_time_utc": time(hour=cadence.best_hour_utc).strftime("%H:%M"),
        "rationale": cadence.rationale,
    }


__all__ = [
    "CADENCES",
    "Cadence",
    "cadence_for",
    "describe",
    "next_slot",
    "suggest_schedule",
    "weekly_capacity",
]
