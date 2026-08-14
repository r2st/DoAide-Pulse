"""Posting cadence and timing suggestions for the calendar.

Deliberately a table of constants rather than a model call. Two reasons: the
published guidance for these platforms is stable and boring, and a suggestion
the user can predict is one they can override with confidence.

This is now the *prior*, not the final answer. Once a user has enough of their
own results on a platform, :mod:`app.services.learned_cadence` derives the
hours from those and passes the result back through :func:`next_slot` as
``using``. The table still stands for every platform where the evidence is thin,
which is most of them for most of a project's life.

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
    Platform.BLUESKY: Cadence(
        platform=Platform.BLUESKY,
        max_per_week=7,
        best_weekdays=(0, 1, 2, 3, 4),
        best_hour_utc=15,
        rationale="Short-form platform with a fast-moving feed — one post per "
        "weekday is comfortable, and mid-afternoon UTC catches both "
        "European and US audiences.",
    ),
    Platform.MASTODON: Cadence(
        platform=Platform.MASTODON,
        max_per_week=5,
        best_weekdays=(0, 1, 2, 3, 4),
        best_hour_utc=14,
        rationale="The federated timeline is chronological, so timing matters "
        "more than on algorithmic feeds. Weekday afternoons UTC "
        "see the most activity.",
    ),
    Platform.BUTTONDOWN: Cadence(
        platform=Platform.BUTTONDOWN,
        max_per_week=1,
        best_weekdays=(1, 3),
        best_hour_utc=14,
        rationale="The only channel where posting too often costs you the "
        "audience permanently — a feed is ignored, an inbox "
        "unsubscribes. Once a week, Tuesday or Thursday morning "
        "US Eastern, is the newsletter consensus.",
    ),
    Platform.GIT: Cadence(
        platform=Platform.GIT,
        max_per_week=3,
        best_weekdays=(1, 2, 3),
        best_hour_utc=13,
        rationale="Static-site deploys on Tue–Thu give search engines a "
        "consistent crawl window and avoid weekend low-traffic periods.",
    ),
}


_DEFAULT_CADENCE_TEMPLATE = Cadence(
    platform=Platform.DEVTO,  # placeholder, overwritten below
    max_per_week=2,
    best_weekdays=(1, 2, 3),
    best_hour_utc=13,
    rationale="No platform-specific guidance yet — defaulting to a "
    "conservative twice-a-week cadence.",
)


def cadence_for(platform: Platform | str) -> Cadence:
    """The posting rhythm guidance for *platform*.

    Never raises for a platform the table has not been updated for: a new
    entry in the enum returns a conservative default instead, because the
    alternative is that adding a platform breaks the calendar for every
    existing one.
    """
    key = platform if isinstance(platform, Platform) else Platform(platform)
    cadence = CADENCES.get(key)
    if cadence is not None:
        return cadence
    # Return a safe default rather than crashing when a new platform is added
    # to the enum before the cadence table is updated.
    return Cadence(
        platform=key,
        max_per_week=_DEFAULT_CADENCE_TEMPLATE.max_per_week,
        best_weekdays=_DEFAULT_CADENCE_TEMPLATE.best_weekdays,
        best_hour_utc=_DEFAULT_CADENCE_TEMPLATE.best_hour_utc,
        rationale=_DEFAULT_CADENCE_TEMPLATE.rationale,
    )


def next_slot(
    platform: Platform | str,
    *,
    after: datetime,
    taken: list[datetime] | None = None,
    using: Cadence | None = None,
) -> datetime:
    """The next good time to post on *platform* after *after*.

    The search starts on *after*'s own day and steps forward a day at a time.
    Today counts whenever its hour has not gone yet: asking at 09:00 on a
    Tuesday — a day dev.to's rota already likes — must not answer "Wednesday"
    and throw away the 13:00 slot four hours away. Every caller passes ``now``
    as *after*, so skipping the current day cost a day off the front of every
    suggestion the calendar and the optimiser made.

    Skips any slot within 12 hours of one already in *taken*, so "schedule the
    next three" spreads them out instead of stacking them on the same morning.
    Searches four weeks ahead and then gives up and returns the last candidate —
    a calendar that silently returns nothing is worse than one that suggests a
    crowded slot.

    *using* substitutes a cadence for the table's, which is how
    :mod:`app.services.learned_cadence` feeds this function hours derived from
    the user's own results. The search itself is identical either way — only
    the hour and the weekdays differ.
    """
    cadence = using or cadence_for(platform)
    occupied = taken or []

    candidate = after.replace(
        hour=cadence.best_hour_utc, minute=0, second=0, microsecond=0
    )
    # Strictly after: a slot exactly on *after* is the moment the caller is
    # already standing in, and "the next good time" is the one after it.
    if candidate <= after:
        candidate += timedelta(days=1)

    for _ in range(28):
        if candidate.weekday() in cadence.best_weekdays and not any(
            abs((candidate - t).total_seconds()) < 12 * 3600 for t in occupied
        ):
            return candidate
        candidate += timedelta(days=1)

    return candidate


def suggest_schedule(
    platform: Platform | str,
    *,
    count: int,
    start: datetime,
    using: Cadence | None = None,
) -> list[datetime]:
    """*count* well-spaced slots for one platform, starting after *start*."""
    slots: list[datetime] = []
    cursor = start
    for _ in range(count):
        slot = next_slot(platform, after=cursor, taken=slots, using=using)
        slots.append(slot)
        cursor = slot
    return slots


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
]
