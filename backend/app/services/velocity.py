"""Reading the metric series, not only its last row.

``content_metrics`` has been append-only since it was introduced (see
:mod:`app.models.metrics`), but every aggregation in
:mod:`app.services.analytics_service` reads exactly one row per publication —
the latest. That answers "how many views does this post have" and nothing
else. The series answers the questions worth acting on: how fast a piece found
its audience, whether it is still finding one, and how its first day compares
with the same platform's usual first day.

Two properties of the data shape everything here:

* **The counters are cumulative.** Dev.to's ``page_views_count`` is lifetime
  views, so a reading at +24h *is* the first day's total, and a gain between
  two moments is a subtraction — never a sum over the snapshots in between,
  which would count the same views once per poll. Same reasoning, and the same
  monotonic clamp, as :func:`app.services.headlines.performance`.

* **Polling is periodic and lossy.** Snapshots land every
  ``metrics_scan_interval_seconds`` (six hours by default), and a platform that
  reports nothing at all leaves a ``NULL``. A window is therefore only
  answerable when a reading actually covers it: the first 24 hours of a post
  whose first snapshot arrived at +30h is ``None``, not zero and not the +30h
  number. ``None`` means unknown here exactly as it does in
  :mod:`app.services.analytics_service`.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus

#: How much of a window a reading must cover before it is allowed to stand for
#: it. Polls are periodic, so the last reading before the 24-hour mark lands
#: somewhere in [18h, 24h] on the default six-hour cadence — comfortably over
#: this bar. What the bar actually rejects is the pathological case: a post
#: polled once at +2h and then not again until +40h, where calling the +2h
#: number "the first day" would understate it by most of a day. A fraction
#: rather than an absolute tolerance, so it stays correct if the poll interval
#: is retuned.
_MIN_WINDOW_COVERAGE = 0.6


@dataclass(frozen=True)
class Point:
    """One reading, positioned relative to the moment of publication."""

    #: Hours since publication. Fractional — polls do not land on the hour.
    hours: float
    #: Cumulative totals as of this reading, not gains since the last one.
    views: int
    engagement: int


@dataclass
class Curve:
    """One publication's growth, as observed.

    ``points`` is in chronological order and monotonic in ``views``: a platform
    counter that goes backwards (a purge, a rescrape, a bot sweep being undone)
    is clamped to its previous value rather than recorded as negative growth,
    because nothing the author did made those views un-happen.
    """

    publication_id: int
    content_id: int
    platform: Platform
    title: str
    published_at: datetime
    #: How long the post has been live, at the moment the curve was built.
    age_hours: float
    points: list[Point] = field(default_factory=list)

    # ---- Reading the curve ------------------------------------------------ #

    def reading_at(self, hours: float) -> Point | None:
        """The last reading taken at or before *hours* since publication."""
        chosen: Point | None = None
        for point in self.points:
            if point.hours > hours:
                break
            chosen = point
        return chosen

    def _covering(self, hours: float) -> Point | None:
        """The reading that may stand for the window ending at *hours*.

        ``None`` when the post is not that old yet, or when the newest reading
        inside the window is too early to represent it — both genuinely
        "unknown" rather than zero.
        """
        if self.age_hours < hours:
            return None
        point = self.reading_at(hours)
        if point is None or point.hours < hours * _MIN_WINDOW_COVERAGE:
            return None
        return point

    def views_within(self, hours: float) -> int | None:
        """Cumulative views in the first *hours*, or ``None`` when unobserved."""
        point = self._covering(hours)
        return point.views if point is not None else None

    def gain_between(self, start_hours: float, end_hours: float) -> int | None:
        """Views gained in ``[start_hours, end_hours]``, or ``None``.

        A subtraction of two cumulative readings. ``None`` if either end of the
        span has no reading behind it — a gain computed against a missing
        baseline is an invention, and the one place it would matter (a post's
        very first window) is handled by :meth:`views_within` instead.
        """
        start = self.reading_at(start_hours)
        end = self.reading_at(end_hours)
        if start is None or end is None:
            return None
        return max(0, end.views - start.views)

    # ---- Decay ------------------------------------------------------------ #

    def peak_gain(self, window_hours: float) -> int | None:
        """The most views this post ever gained in any span of *window_hours*.

        The yardstick a quiet week is measured against. ``None`` with fewer
        than two readings, which is not enough to observe a gain at all.
        """
        if len(self.points) < 2:
            return None
        best = 0
        end = 0
        for start, point in enumerate(self.points):
            if end < start:
                end = start
            while (
                end + 1 < len(self.points)
                and self.points[end + 1].hours - point.hours <= window_hours
            ):
                end += 1
            best = max(best, self.points[end].views - point.views)
        return best

    def recent_gain(self, window_hours: float) -> int | None:
        """Views gained in the last *window_hours* of observation.

        Measured back from the newest reading rather than from "now": a post
        whose last poll was three days ago has not been observed since, and
        counting that silence as zero growth would report every post as dead
        the moment metrics collection stops.
        """
        if len(self.points) < 2:
            return None
        latest = self.points[-1]
        return self.gain_between(latest.hours - window_hours, latest.hours)

    def is_stalled(self, *, window_hours: float, ratio: float) -> bool:
        """True when this post has all but stopped growing.

        Deliberately conservative. It needs two full windows of history (so a
        post cannot be declared dead on its first day), a peak that was
        actually non-trivial (so a piece that never found anyone is "never
        landed", a different problem), and a recent window at or under *ratio*
        of that peak.
        """
        if self.age_hours < window_hours * 2:
            return False
        peak = self.peak_gain(window_hours)
        recent = self.recent_gain(window_hours)
        if not peak or recent is None:
            return False
        return recent <= peak * ratio

    def as_dict(self) -> dict:
        early = float(settings.velocity_early_window_hours)
        benchmark = float(settings.velocity_benchmark_window_hours)
        latest = self.points[-1] if self.points else None
        return {
            "publication_id": self.publication_id,
            "content_id": self.content_id,
            "platform": self.platform.value,
            "title": self.title,
            "published_at": self.published_at,
            "age_hours": round(self.age_hours, 1),
            "snapshots": len(self.points),
            "views": latest.views if latest else 0,
            "engagement": latest.engagement if latest else 0,
            f"views_first_{int(early)}h": self.views_within(early),
            f"views_first_{int(benchmark)}h": self.views_within(benchmark),
            "views_per_day": self.views_per_day(),
            "stalled": self.is_stalled(
                window_hours=float(settings.velocity_stall_window_hours),
                ratio=settings.velocity_stall_ratio,
            ),
        }

    def views_per_day(self) -> float | None:
        """Lifetime views divided by days live — the crude comparable rate."""
        if not self.points or self.age_hours <= 0:
            return None
        return round(self.points[-1].views / (self.age_hours / 24), 2)


def _build_curve(
    publication: Publication,
    content: Content,
    metrics: list[ContentMetric],
    *,
    now: datetime,
) -> Curve:
    published_at = as_aware(publication.published_at)
    points: list[Point] = []
    views = engagement = 0
    for metric in metrics:
        # A NULL views column means "this platform said nothing this time", so
        # the previous total carries forward; a lower number means the counter
        # moved backwards and is clamped. Either way the series stays monotonic,
        # which is what makes a subtraction between any two points meaningful.
        views = max(views, metric.views if metric.views is not None else views)
        engagement = max(engagement, metric.engagement)
        hours = (as_aware(metric.captured_at) - published_at).total_seconds() / 3600
        if hours < 0:
            # A snapshot taken before the recorded publish time: the publish
            # timestamp was backdated by an import or a manual fix. Position it
            # at zero rather than dropping it — it is still the earliest known
            # reading, and a negative offset would corrupt every window.
            hours = 0.0
        points.append(Point(hours=hours, views=views, engagement=engagement))

    return Curve(
        publication_id=publication.id,
        content_id=publication.content_id,
        platform=publication.platform,
        title=content.title,
        published_at=published_at,
        age_hours=max(0.0, (now - published_at).total_seconds() / 3600),
        points=points,
    )


def curves(
    db: Session,
    user_id: int,
    *,
    project_id: int | None = None,
    platform: Platform | None = None,
    now: datetime | None = None,
) -> list[Curve]:
    """A growth curve per published publication, newest publication first.

    Publications with no ``published_at`` are skipped rather than defaulted to
    their first snapshot: every window here is measured from the moment the post
    went live, and a guessed origin would silently shift all of them.

    Two queries regardless of how many publications there are — the snapshots
    are fetched in one pass and grouped in Python, because the alternative is a
    round trip per post.
    """
    moment = now or utcnow()

    query = (
        select(Publication, Content)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.published_at.is_not(None),
        )
    )
    if project_id is not None:
        query = query.where(Content.project_id == project_id)
    if platform is not None:
        query = query.where(Publication.platform == platform)

    rows = db.execute(query).all()
    if not rows:
        return []

    by_publication: dict[int, list[ContentMetric]] = {pub.id: [] for pub, _ in rows}
    snapshots = db.scalars(
        select(ContentMetric)
        .where(ContentMetric.publication_id.in_(by_publication))
        # Per publication, then in time order: every subtraction below is only
        # meaningful against the same publication's previous reading.
        .order_by(ContentMetric.publication_id, ContentMetric.captured_at)
    )
    for metric in snapshots:
        by_publication[metric.publication_id].append(metric)

    built = [
        _build_curve(pub, content, by_publication[pub.id], now=moment)
        for pub, content in rows
    ]
    return sorted(built, key=lambda c: c.published_at, reverse=True)


def _median(values: list[int]) -> float | None:
    """Median, or ``None`` for an empty sample. Median, not mean, throughout.

    One post that got picked up by an aggregator is worth more views than
    everything else combined, and a mean would then declare every subsequent
    post a failure.
    """
    return round(statistics.median(values), 1) if values else None


@dataclass(frozen=True)
class Benchmark:
    """What a normal first day looks like on one platform, for this user."""

    platform: Platform
    #: Publications that supplied an observation for each window.
    early_sample: int
    benchmark_sample: int
    median_early_views: float | None
    median_benchmark_views: float | None
    #: True once the sample is large enough to compare an individual post
    #: against. Below it the medians are still reported — they are interesting —
    #: but nothing is judged against them.
    reliable: bool

    def as_dict(self) -> dict:
        return {
            "platform": self.platform.value,
            "early_window_hours": settings.velocity_early_window_hours,
            "benchmark_window_hours": settings.velocity_benchmark_window_hours,
            "median_early_views": self.median_early_views,
            "median_benchmark_views": self.median_benchmark_views,
            "early_sample": self.early_sample,
            "benchmark_sample": self.benchmark_sample,
            "reliable": self.reliable,
        }


def benchmarks(
    db: Session, user_id: int, *, known: list[Curve] | None = None
) -> list[Benchmark]:
    """Per-platform medians for the two early windows.

    Pass *known* to reuse curves the caller already built — the alert pass and
    the dashboard both want these numbers and the query is not free.
    """
    early = float(settings.velocity_early_window_hours)
    window = float(settings.velocity_benchmark_window_hours)
    all_curves = known if known is not None else curves(db, user_id)

    grouped: dict[Platform, list[Curve]] = {}
    for curve in all_curves:
        grouped.setdefault(curve.platform, []).append(curve)

    out: list[Benchmark] = []
    for platform, group in grouped.items():
        early_views = [v for v in (c.views_within(early) for c in group) if v is not None]
        window_views = [v for v in (c.views_within(window) for c in group) if v is not None]
        out.append(
            Benchmark(
                platform=platform,
                early_sample=len(early_views),
                benchmark_sample=len(window_views),
                median_early_views=_median(early_views),
                median_benchmark_views=_median(window_views),
                reliable=len(window_views) >= settings.velocity_min_sample,
            )
        )
    return sorted(out, key=lambda b: b.platform.value)


def benchmark_excluding(
    group: list[Curve], subject: Curve, *, hours: float
) -> float | None:
    """The platform's median for *hours*, ignoring *subject*'s own contribution.

    Leave-one-out, because the alternative is circular: with four posts on a
    platform, a post being judged is a quarter of the median it is judged
    against, so a bad one drags down its own bar and looks less bad than it is.
    Returns ``None`` until the remaining sample clears
    ``velocity_min_sample`` — a verdict from two other posts is not a verdict.
    """
    others = [
        value
        for curve in group
        if curve.publication_id != subject.publication_id
        for value in (curve.views_within(hours),)
        if value is not None
    ]
    if len(others) < settings.velocity_min_sample:
        return None
    return _median(others)


def summary(db: Session, user_id: int) -> dict:
    """Everything the velocity panel shows, in one pass over the series."""
    all_curves = curves(db, user_id)
    early = float(settings.velocity_early_window_hours)

    fastest = sorted(
        (c for c in all_curves if c.views_within(early) is not None),
        key=lambda c: c.views_within(early) or 0,
        reverse=True,
    )[:5]
    stalled = [
        c
        for c in all_curves
        if c.is_stalled(
            window_hours=float(settings.velocity_stall_window_hours),
            ratio=settings.velocity_stall_ratio,
        )
    ]

    return {
        "early_window_hours": settings.velocity_early_window_hours,
        "benchmark_window_hours": settings.velocity_benchmark_window_hours,
        "publications": len(all_curves),
        "benchmarks": [b.as_dict() for b in benchmarks(db, user_id, known=all_curves)],
        "fastest": [c.as_dict() for c in fastest],
        "stalled": [c.as_dict() for c in stalled[:10]],
    }


__all__ = [
    "Benchmark",
    "Curve",
    "Point",
    "benchmark_excluding",
    "benchmarks",
    "curves",
    "summary",
]
