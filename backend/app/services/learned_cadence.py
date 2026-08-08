"""Posting times derived from the user's own results, not from a table.

:mod:`app.services.cadence` opens with a promise: the constants stand in until
Herald has enough of the user's *own* metrics to beat them. This is that.

The table is generic advice — "Tue–Thu, 13:00 UTC, because that is mid-morning
US Eastern". It is a reasonable prior and a poor posterior. A user whose
audience is in Berlin, or who writes for a niche that reads on Sunday
afternoons, is being told to post at the wrong time by a constant that cannot
learn. Their own publications already say when their audience shows up; that
signal has simply never been read.

**What is learned, and what is not.** Only the *when*: best hour, best
weekdays. ``max_per_week`` stays from the table, because it encodes how much
posting an audience tolerates before it tunes out — a judgement about
saturation that view counts cannot express. Metrics say which post did well;
they never say which post would not have been sent at all.

**The bar for replacing the table.** A learned answer needs
``learned_cadence_min_samples`` observations on that platform, and the winning
hour needs ``learned_cadence_min_bucket`` posts of its own. Below either, the
table stands unchanged. Two posts at 09:00 that happened to do well are a
coincidence, and a scheduler that chases coincidences is worse than one that is
merely generic — its suggestions move every week and stop being predictable,
which was the original argument for a table.

The comparable is each post's first-``velocity_early_window_hours`` views (see
:mod:`app.services.velocity`), not lifetime views: a post from March has had
five months to accumulate and would win every bucket it landed in on age
alone.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import time

from sqlalchemy.orm import Session

from app.config import settings
from app.models.mixins import as_aware
from app.models.publication import Platform
from app.services import cadence, velocity
from app.services.cadence import Cadence

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class Learned:
    """One platform's cadence, and where each part of it came from."""

    cadence: Cadence
    #: True when the hours below were derived from this user's results.
    is_learned: bool
    #: Observations behind the answer, and behind the winning hour alone.
    sample: int
    best_hour_sample: int = 0
    #: Median early views for the winning hour, and across every hour. The pair
    #: is what makes the suggestion arguable rather than oracular.
    best_hour_median: float | None = None
    overall_median: float | None = None

    def as_dict(self) -> dict:
        described = cadence.describe(self.cadence.platform)
        described.update(
            {
                "best_weekdays": [_DAY_NAMES[d] for d in self.cadence.best_weekdays],
                "best_time_utc": time(hour=self.cadence.best_hour_utc).strftime("%H:%M"),
                "rationale": self.cadence.rationale,
                "source": "learned" if self.is_learned else "table",
                "sample": self.sample,
                "best_hour_sample": self.best_hour_sample,
                "best_hour_median_views": self.best_hour_median,
                "overall_median_views": self.overall_median,
            }
        )
        return described


@dataclass(frozen=True)
class _Observation:
    """One published post reduced to "when it went out, how it started"."""

    hour: int
    weekday: int
    early_views: int


def observations(
    db: Session, user_id: int, platform: Platform, *, known: list[velocity.Curve] | None = None
) -> list[_Observation]:
    """Every post on *platform* that has a readable first-window number.

    Posts still inside their first window, and posts no poll covered, are
    absent rather than zero — see :meth:`app.services.velocity.Curve.views_within`.
    """
    window = float(settings.velocity_early_window_hours)
    curves = (
        [c for c in known if c.platform == platform]
        if known is not None
        else velocity.curves(db, user_id, platform=platform)
    )

    out: list[_Observation] = []
    for curve in curves:
        views = curve.views_within(window)
        if views is None:
            continue
        published = as_aware(curve.published_at)
        out.append(
            _Observation(
                hour=published.hour, weekday=published.weekday(), early_views=views
            )
        )
    return out


def _best_bucket(
    grouped: dict[int, list[int]], *, min_bucket: int
) -> tuple[int, float, int] | None:
    """The key with the highest median, given enough observations under it.

    Ties break toward the *earlier* key, which for hours means the earlier slot
    and for weekdays the earlier day. Arbitrary, but stable — an unstable
    tiebreak would move a user's suggested time whenever a poll landed.
    """
    ranked = [
        (key, statistics.median(values), len(values))
        for key, values in sorted(grouped.items())
        if len(values) >= min_bucket
    ]
    if not ranked:
        return None
    return max(ranked, key=lambda row: row[1])


def learn(
    db: Session,
    user_id: int,
    platform: Platform | str,
    *,
    known: list[velocity.Curve] | None = None,
) -> Learned:
    """The best cadence available for *platform* — learned if possible, table if not.

    Never raises and never returns nothing: every caller is choosing a time to
    publish, and "no answer" is not a usable one.
    """
    key = platform if isinstance(platform, Platform) else Platform(platform)
    table = cadence.cadence_for(key)

    if not settings.learned_cadence_enabled:
        return Learned(cadence=table, is_learned=False, sample=0)

    sample = observations(db, user_id, key, known=known)
    if len(sample) < settings.learned_cadence_min_samples:
        return Learned(cadence=table, is_learned=False, sample=len(sample))

    by_hour: dict[int, list[int]] = {}
    by_weekday: dict[int, list[int]] = {}
    for observation in sample:
        by_hour.setdefault(observation.hour, []).append(observation.early_views)
        by_weekday.setdefault(observation.weekday, []).append(observation.early_views)

    best_hour = _best_bucket(by_hour, min_bucket=settings.learned_cadence_min_bucket)
    if best_hour is None:
        # Enough posts overall, but scattered across too many hours to name one.
        # The table's hour is as good as anything the data supports.
        return Learned(cadence=table, is_learned=False, sample=len(sample))

    hour, hour_median, hour_count = best_hour
    overall_median = statistics.median([o.early_views for o in sample])

    # Weekdays are a set, not a winner: several days can be worth posting on,
    # and narrowing to one would push every piece in a queue a week apart. Any
    # day that beats the overall median qualifies; if none does — which happens
    # when one day carries everything — keep the table's days rather than
    # returning an empty tuple that would make next_slot search for ever.
    good_days = tuple(
        day
        for day, values in sorted(by_weekday.items())
        if statistics.median(values) >= overall_median
    )
    weekdays = good_days or table.best_weekdays

    return Learned(
        cadence=Cadence(
            platform=key,
            max_per_week=table.max_per_week,
            best_weekdays=weekdays,
            best_hour_utc=hour,
            rationale=(
                f"Learned from {len(sample)} of your posts on {key.value}: "
                f"{hour:02d}:00 UTC is your best start, with a median "
                f"{hour_median:,.0f} first-day views across {hour_count} "
                f"post{'s' if hour_count != 1 else ''} against "
                f"{overall_median:,.0f} overall."
            ),
        ),
        is_learned=True,
        sample=len(sample),
        best_hour_sample=hour_count,
        best_hour_median=round(hour_median, 1),
        overall_median=round(overall_median, 1),
    )


def cadence_for(db: Session, user_id: int, platform: Platform | str) -> Cadence:
    """Drop-in replacement for :func:`app.services.cadence.cadence_for`."""
    return learn(db, user_id, platform).cadence


def describe_all(
    db: Session,
    user_id: int,
    platforms: list[Platform | str],
    *,
    known: list[velocity.Curve] | None = None,
) -> list[dict]:
    """Cadence guidance for several platforms, sharing one pass over the series.

    Pass *known* when the caller has already built the curves — same contract as
    :func:`learn` and :func:`benchmarks`. The calendar endpoint builds them to
    pick its suggested slots and then asks for this, and without somewhere to
    hand them over it paid for the whole metric series twice on every load.
    """
    wanted = [p if isinstance(p, Platform) else Platform(p) for p in platforms]
    if not wanted:
        return []
    if known is None:
        known = velocity.curves(db, user_id) if settings.learned_cadence_enabled else []
    return [learn(db, user_id, p, known=known).as_dict() for p in wanted]


__all__ = ["Learned", "cadence_for", "describe_all", "learn", "observations"]
