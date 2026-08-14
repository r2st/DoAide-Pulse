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
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
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


def window_count_key(hours: int) -> str:
    """The wire name for a curve's view count over its first *hours* hours.

    The single definition of these two names. :meth:`Curve.as_dict` builds its
    keys with it and :mod:`app.schemas.analytics` both documents and bounds them
    with it — two places that would otherwise agree only by having the same
    f-string written out in each, which is the arrangement that lets a rename
    reach production as a pair of silently-dropped fields.
    """
    return f"views_first_{hours}h"


@dataclass(frozen=True)
class Point:
    """One reading, positioned relative to the moment of publication."""

    #: Hours since publication. Fractional — polls do not land on the hour.
    hours: float
    #: Cumulative totals as of this reading, not gains since the last one.
    views: int
    engagement: int


@dataclass(frozen=True)
class Lifetime:
    """A whole series reduced to the four numbers a summary row needs from it.

    :meth:`Curve.as_dict` describes a post's whole life: how many times it was
    polled, where its counters ended up, and whether it has stopped growing.
    None of that can be read off a curve built with ``within_hours`` — which is
    why :meth:`Curve._require_full_series` refuses it — but all of it can be
    *computed by the database*, because the monotonic clamp makes every one of
    these an aggregate. :func:`_series_facts` computes them.

    Attaching one to a truncated curve is therefore not a way around the guard;
    it is the answer the guard demanded, arriving from somewhere other than a
    list of rows in memory. A curve holding one may describe itself; a curve
    holding neither the tail nor this still may not.
    """

    #: How many readings the whole series holds — ``len(Curve.points)`` would
    #: undercount it on a truncated curve.
    snapshots: int
    #: Clamped cumulative totals at the newest reading.
    views: int
    engagement: int
    #: The verdict from :meth:`Curve.is_stalled`, settled against the whole
    #: series one way or another — see :func:`_stall_verdict`.
    stalled: bool


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
    #: Set when the series was deliberately read only through this many hours
    #: past publication — see the ``within_hours`` argument to :func:`curves`.
    #: ``None`` means the whole series is here. Every method below that would
    #: answer differently on a truncated series checks this and refuses, because
    #: the failure mode otherwise is silent: a post with a year of history read
    #: through its first day looks like a post that stopped growing after a day.
    observed_hours: float | None = None
    #: The whole-series numbers, when the caller has them from somewhere other
    #: than ``points`` — see :class:`Lifetime`. ``None`` on a curve that either
    #: holds its own tail or has no business describing itself.
    lifetime: Lifetime | None = None

    # ---- Reading the curve ------------------------------------------------ #

    def _require_full_series(self, question: str) -> None:
        if self.observed_hours is not None:
            raise ValueError(
                f"{question} needs the whole series, but this curve was built "
                f"with within_hours={self.observed_hours:g}. Rebuild it with "
                "velocity.curves(...) and no window."
            )

    def reading_at(self, hours: float) -> Point | None:
        """The last reading taken at or before *hours* since publication."""
        if self.observed_hours is not None and hours > self.observed_hours:
            raise ValueError(
                f"asked for the reading at {hours:g}h, but this curve was only "
                f"read through {self.observed_hours:g}h"
            )
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

    def observed_peak_gain(self, window_hours: float) -> int:
        """The best gain over any *window_hours* span **among the readings held**.

        A lower bound on :meth:`peak_gain`, and equal to it whenever the series
        is whole — it maximises over the same pairs of readings, just only the
        ones this curve happens to have. That is why this one does not call
        :meth:`_require_full_series`: a truncated curve cannot say what the peak
        *was*, but it can honestly say the peak was *at least* this, and the
        name says which of the two is on offer.

        The bound is what lets :func:`summary` settle most stall verdicts
        without reading a year of snapshots — see :func:`_stall_verdict`. Zero
        with fewer than two readings, which is a true lower bound and not a
        claim that nothing was gained; the caller that needs "unknown" told
        apart from "none" wants :meth:`peak_gain`.
        """
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

    def peak_gain(self, window_hours: float) -> int | None:
        """The most views this post ever gained in any span of *window_hours*.

        The yardstick a quiet week is measured against. ``None`` with fewer
        than two readings, which is not enough to observe a gain at all.
        """
        self._require_full_series("peak_gain")
        if len(self.points) < 2:
            return None
        return self.observed_peak_gain(window_hours)

    def recent_gain(self, window_hours: float) -> int | None:
        """Views gained in the last *window_hours* of observation.

        Measured back from the newest reading rather than from "now": a post
        whose last poll was three days ago has not been observed since, and
        counting that silence as zero growth would report every post as dead
        the moment metrics collection stops.
        """
        self._require_full_series("recent_gain")
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
        self._require_full_series("is_stalled")
        if self.age_hours < window_hours * 2:
            return False
        peak = self.peak_gain(window_hours)
        recent = self.recent_gain(window_hours)
        if not peak or recent is None:
            return False
        return recent <= peak * ratio

    def _whole_life(self, question: str) -> Lifetime:
        """The lifetime numbers, from an attached :class:`Lifetime` or the tail.

        The single place the two sources meet, so the curve that reads its own
        series and the curve handed the database's aggregates of it cannot
        answer differently. A curve with neither still refuses — that is
        :meth:`_require_full_series` doing its job.
        """
        if self.lifetime is not None:
            return self.lifetime
        self._require_full_series(question)
        latest = self.points[-1] if self.points else None
        return Lifetime(
            snapshots=len(self.points),
            views=latest.views if latest else 0,
            engagement=latest.engagement if latest else 0,
            stalled=self.is_stalled(
                window_hours=float(settings.velocity_stall_window_hours),
                ratio=settings.velocity_stall_ratio,
            ),
        )

    def as_dict(self) -> dict:
        """The wire shape of one curve.

        Two of these keys are named from configuration —
        ``views_first_{early}h`` and ``views_first_{benchmark}h`` — which is why
        :class:`app.schemas.analytics.VelocityCurveOut` cannot declare them as
        fields. The name is built by
        :func:`app.schemas.analytics.window_count_key` so that the schema which
        documents these keys and the code which produces them cannot drift
        apart; the windows themselves ship alongside, so a client holding one
        curve can work out which two keys it is looking at without fetching the
        summary as well.
        """
        life = self._whole_life("as_dict")
        early = int(settings.velocity_early_window_hours)
        benchmark = int(settings.velocity_benchmark_window_hours)
        return {
            "publication_id": self.publication_id,
            "content_id": self.content_id,
            "platform": self.platform.value,
            "title": self.title,
            "published_at": self.published_at,
            "age_hours": round(self.age_hours, 1),
            "snapshots": life.snapshots,
            "views": life.views,
            "engagement": life.engagement,
            "early_window_hours": early,
            "benchmark_window_hours": benchmark,
            window_count_key(early): self.views_within(float(early)),
            window_count_key(benchmark): self.views_within(float(benchmark)),
            "views_per_day": self.views_per_day(),
            "stalled": life.stalled,
        }

    def views_per_day(self) -> float | None:
        """Lifetime views divided by days live — the crude comparable rate."""
        life = self._whole_life("views_per_day")
        if not life.snapshots or self.age_hours <= 0:
            return None
        return round(life.views / (self.age_hours / 24), 2)


def _build_curve(
    publication: Publication,
    content: Content,
    metrics: list[ContentMetric],
    *,
    now: datetime,
    within_hours: float | None = None,
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
        observed_hours=within_hours,
    )


def curves(
    db: Session,
    user_id: int,
    *,
    project_id: int | None = None,
    platform: Platform | None = None,
    publication_id: int | None = None,
    publication_ids: Collection[int] | None = None,
    now: datetime | None = None,
    within_hours: float | None = None,
) -> list[Curve]:
    """A growth curve per published publication, newest publication first.

    Publications with no ``published_at`` are skipped rather than defaulted to
    their first snapshot: every window here is measured from the moment the post
    went live, and a guessed origin would silently shift all of them.

    Pass *publication_id* when only one curve is wanted. The filter belongs in
    the query rather than in the caller: the detail endpoint used to build every
    curve on the account and then keep one, so opening a chart for a single post
    read the whole snapshot history of every post the account had ever
    published. It stays a ``list`` — empty when the id is not this user's, which
    is what makes "not yours" and "not there" the same answer there.

    Pass *publication_ids* for the same reason with more than one id — the shape
    :func:`summary` needs, where the whole series is wanted for the handful of
    publications it will actually render and for nothing else. Both filters are
    ``AND``-ed when both are given, and an empty collection selects nothing
    rather than everything, because the caller that computed an empty shortlist
    meant an empty shortlist.

    Two queries regardless of how many publications there are — the snapshots
    are fetched in one pass and grouped in Python, because the alternative is a
    round trip per post.

    Pass *within_hours* when the caller only ever asks the curve about the first
    N hours of a post's life. ``content_metrics`` is append-only and nothing
    prunes it, so a post polled every six hours for a year carries some 1,400
    rows; reading all of them to answer "how did its first day go" made the
    calendar's cost scale with how long the account had existed rather than with
    what it was being asked. Bounded, that same post contributes four rows.

    The bound is per publication and relative to its own ``published_at``, so it
    cannot be one ``WHERE`` clause: it is an ``OR`` of one predicate per
    publication, which the ``(publication_id, captured_at)`` index answers a
    disjunct at a time. That trades a statement proportional to the number of
    publications for a result set that no longer grows with the age of the
    account, and publications are the far smaller and slower-growing number.

    Curves built this way carry :attr:`Curve.observed_hours` and refuse the
    questions a truncated series cannot honestly answer — see there.
    """
    moment = now or utcnow()
    if publication_ids is not None and not publication_ids:
        return []

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
    if publication_id is not None:
        query = query.where(Publication.id == publication_id)
    if publication_ids is not None:
        query = query.where(Publication.id.in_(publication_ids))

    rows = db.execute(query).all()
    if not rows:
        return []

    by_publication: dict[int, list[ContentMetric]] = {pub.id: [] for pub, _ in rows}
    if within_hours is None:
        wanted = ContentMetric.publication_id.in_(by_publication)
    else:
        span = timedelta(hours=within_hours)
        wanted = or_(
            *(
                and_(
                    ContentMetric.publication_id == pub.id,
                    ContentMetric.captured_at <= as_aware(pub.published_at) + span,
                )
                for pub, _ in rows
            )
        )
    snapshots = db.scalars(
        select(ContentMetric)
        .where(wanted)
        # Per publication, then in time order: every subtraction below is only
        # meaningful against the same publication's previous reading.
        .order_by(ContentMetric.publication_id, ContentMetric.captured_at)
    )
    for metric in snapshots:
        by_publication[metric.publication_id].append(metric)

    built = [
        _build_curve(
            pub, content, by_publication[pub.id], now=moment, within_hours=within_hours
        )
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
        """The wire shape of one platform's benchmark.

        The two window sizes ship alongside the medians so a client holding this
        knows what "early" and "benchmark" meant on the install that produced
        it — both are configurable, and a median with no window attached to it
        cannot be read.
        """
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


#: How many of each list the velocity panel shows.
_FASTEST_SHOWN = 5
_STALLED_SHOWN = 10


@dataclass(frozen=True)
class _SeriesFacts:
    """What the stall check needs from a whole series, without reading it.

    Every field here is an aggregate the database can compute over the
    ``(publication_id, captured_at)`` index, so a publication contributes one
    row to each of the two queries in :func:`_series_facts` no matter how long
    it has been polled. The monotonic clamp in :func:`_build_curve` is what
    makes this possible: because the running series is a running ``MAX``, the
    clamped value at any moment ``T`` is exactly ``MAX(views)`` over the rows
    captured at or before ``T`` — a fact the database can answer and Python
    would otherwise have to rebuild row by row.
    """

    #: ``len(Curve.points)``: the readings the full curve would hold.
    snapshots: int
    #: Clamped cumulative totals at the newest reading.
    total_views: int
    total_engagement: int
    #: Clamped cumulative views one stall window before the newest reading.
    #: ``None`` when no reading is that old, which is exactly the case
    #: :meth:`Curve.recent_gain` answers ``None`` for.
    baseline_views: int | None


def _series_facts(
    db: Session, known: list[Curve], *, window_hours: float
) -> dict[int, _SeriesFacts]:
    """Aggregate each publication's whole series into a handful of numbers.

    Two queries for the whole account, each returning one row per publication.
    The second one needs the first one's answer — the recent window is measured
    back from each publication's *newest reading* rather than from now (see
    :meth:`Curve.recent_gain`), so the cutoff is per publication and cannot be
    known before ``MAX(captured_at)`` is.

    That per-publication cutoff is an ``OR`` of one predicate each, the same
    shape and for the same reason as the ``within_hours`` bound in
    :func:`curves`.
    """
    ids = [curve.publication_id for curve in known]
    if not ids:
        return {}

    # The same arithmetic as `ContentMetric.engagement`, in the database: every
    # interaction the platform reported, treating a missing one as zero. The
    # sum has to happen inside the MAX — a per-column maximum would mix
    # readings and could exceed anything the platform ever actually reported.
    engagement = func.max(
        func.coalesce(ContentMetric.reactions, 0)
        + func.coalesce(ContentMetric.comments, 0)
        + func.coalesce(ContentMetric.clicks, 0)
        + func.coalesce(ContentMetric.shares, 0)
    )
    rolled = db.execute(
        select(
            ContentMetric.publication_id,
            func.count(ContentMetric.id),
            func.max(ContentMetric.captured_at),
            func.max(ContentMetric.views),
            engagement,
        )
        .where(ContentMetric.publication_id.in_(ids))
        .group_by(ContentMetric.publication_id)
    ).all()
    # ``MAX(views)`` is NULL for a publication whose every reading was NULL —
    # the platform reported nothing, ever. That is a total of zero, the same
    # number the clamp in `_build_curve` carries forward.
    head = {
        row[0]: (row[1], as_aware(row[2]), row[3] or 0, row[4] or 0) for row in rolled
    }

    published_at = {curve.publication_id: curve.published_at for curve in known}
    cutoffs: dict[int, datetime] = {}
    for publication_id, (_, last, _, _) in head.items():
        origin = published_at[publication_id]
        # Positioned the way `_build_curve` positions a reading, so the cutoff
        # lands on the same reading `Curve.reading_at` would pick: a snapshot
        # captured before the recorded publish time sits at hour zero, not at a
        # negative offset.
        latest_hours = max(0.0, (last - origin).total_seconds() / 3600)
        if latest_hours < window_hours:
            # No reading is a full window old, so there is no baseline to
            # subtract and the recent gain is unknown rather than zero.
            continue
        cutoffs[publication_id] = origin + timedelta(hours=latest_hours - window_hours)

    baselines: dict[int, int] = {}
    if cutoffs:
        baselines = {
            publication_id: value or 0
            for publication_id, value in db.execute(
                select(ContentMetric.publication_id, func.max(ContentMetric.views))
                .where(
                    or_(
                        *(
                            and_(
                                ContentMetric.publication_id == publication_id,
                                ContentMetric.captured_at <= cutoff,
                            )
                            for publication_id, cutoff in cutoffs.items()
                        )
                    )
                )
                .group_by(ContentMetric.publication_id)
            ).all()
        }

    return {
        publication_id: _SeriesFacts(
            snapshots=count,
            total_views=total,
            total_engagement=engaged,
            baseline_views=baselines.get(publication_id),
        )
        for publication_id, (count, _, total, engaged) in head.items()
    }


def _stall_verdict(
    prefix: Curve, facts: _SeriesFacts | None, *, window_hours: float, ratio: float
) -> bool | None:
    """What :meth:`Curve.is_stalled` would say, or ``None`` if it takes the tail.

    :meth:`Curve.is_stalled` asks whether the most recent window came in at or
    under *ratio* of the best window this post ever had. The recent window is
    cheap — it is two clamped readings, and :func:`_series_facts` has both
    exactly. The peak is the expensive half, because "ever" means every pair of
    readings in the post's life.

    So the peak is bracketed rather than computed, and most verdicts fall
    outside the bracket:

    * **Above it.** ``peak_gain`` can never exceed the gain across the whole
      observed series, so a recent window larger than *ratio* of that is not
      stalled, whatever the peak turns out to be.
    * **Below it.** Any span this curve's prefix does hold is a peak the post
      really had (:meth:`Curve.observed_peak_gain`), as is the recent window
      itself. A recent window at or under *ratio* of any lower bound is at or
      under *ratio* of the true peak.

    Every lower bound has to be a gain **between two readings no more than
    *window_hours* apart**, because that is the only kind of gain
    :meth:`Curve.peak_gain` counts. That rules out the average — the total gain
    divided by the number of windows it is spread over — however much it looks
    like a pigeonhole argument. Views gained across a gap in polling wider than
    the window are in the total and in no window at all: a post that sat
    unpolled for a fortnight while it collected a thousand views, and has
    trickled since, has a real peak of the trickle and an "average window" of a
    hundred. Judged against the average it is stalled; judged against its peak,
    which is what :meth:`Curve.is_stalled` does, it is not. The bound held for
    every densely-polled series and quietly inverted the verdict for the others,
    which is the shape a wrong bound takes: right until the data is unusual, and
    then confidently wrong. ``tests.test_velocity_summary_budget`` pins the
    series that caught it.

    ``None`` is returned only when the recent window lands between the two
    bounds: the post grew steadily enough that where its best week sat actually
    decides the answer. :func:`summary` reads the whole series for those, which
    is the only honest thing to do and, on a real account, a short list.

    Every branch here returns what the full-series check would return. The
    guarantee is asserted directly in :mod:`tests.test_velocity_summary_budget`,
    against the real :meth:`Curve.is_stalled`, because a bound that is merely
    plausible is a wrong answer waiting for the right data.
    """
    if prefix.age_hours < window_hours * 2:
        return False  # Not two full windows old; no verdict, same as is_stalled.
    if facts is None or facts.snapshots < 2 or facts.baseline_views is None:
        return False  # `peak_gain` or `recent_gain` would be None.

    recent = max(0, facts.total_views - facts.baseline_views)

    # The first reading the prefix holds is the first reading there is, unless
    # the first poll landed after the prefix window — in which case zero is
    # still a floor for it, and a floor is what an upper bound on the gain
    # needs.
    first_views = prefix.points[0].views if prefix.points else 0
    at_most = max(0, facts.total_views - first_views)
    if at_most == 0 or recent > at_most * ratio:
        return False

    at_least = max(prefix.observed_peak_gain(window_hours), recent)
    if at_least > 0 and recent <= at_least * ratio:
        return True
    return None


def summary(db: Session, user_id: int) -> dict:
    """Everything the velocity panel shows, without reading every snapshot.

    Three of the four things on this panel are questions about the beginning of
    a post's life: ``publications`` counts them, ``benchmarks`` medians their
    first two windows, and ``fastest`` ranks them by the first. The fourth,
    ``stalled``, is a question about the end, and it used to drag the other
    three along with it — ``summary`` built every curve on the account whole, so
    a post polled every six hours for a year put some 1,400 rows through the
    dashboard to contribute one number to a median. The panel's cost tracked how
    long the account had been open rather than how much was on it, and this was
    the last read in the service that did.

    So it is done in passes, each bounded:

    #. **A prefix of every publication**, ``max(early, benchmark)`` hours of it —
       four readings on the default cadence. Everything ``publications``,
       ``benchmarks`` and the ``fastest`` ranking need, and nothing else. These
       curves carry :attr:`Curve.observed_hours` and refuse anything further.
    #. **Two aggregates over the whole series**, one row per publication. Most
       stall verdicts follow from these without reading a single snapshot (see
       :func:`_stall_verdict`), and so does every lifetime number the rendered
       rows carry (see :class:`Lifetime`) — the snapshot count, the final
       counters, the views-per-day. That is what keeps pass 3 from having to
       happen for the rows that are merely *shown*.
    #. **The whole series, only where the bracket could not settle a verdict.**
       A post that grew steadily enough that where its best week sat decides the
       answer gets read in full, because nothing cheaper is honest. Usually
       nobody is on this list.

    The answers are identical to reading everything, and the tests assert that
    against the unbounded computation rather than against a fixture. What
    changed is that the rows read no longer grow with how long the account has
    been open.
    """
    early = float(settings.velocity_early_window_hours)
    benchmark = float(settings.velocity_benchmark_window_hours)
    window = float(settings.velocity_stall_window_hours)
    ratio = settings.velocity_stall_ratio
    # One moment for every pass, so `age_hours` cannot differ between a curve
    # built in pass 1 and the same curve rebuilt in pass 3 — which would let a
    # publication sit on the far side of the `age_hours < window * 2` gate in
    # one pass and the near side in the next.
    moment = utcnow()

    shape = {
        "early_window_hours": settings.velocity_early_window_hours,
        "benchmark_window_hours": settings.velocity_benchmark_window_hours,
    }

    prefixes = curves(
        db, user_id, now=moment, within_hours=max(early, benchmark)
    )
    if not prefixes:
        return {**shape, "publications": 0, "benchmarks": [], "fastest": [], "stalled": []}

    # `sorted` is stable, so ties here keep the newest-first order `curves`
    # returned — the tie-break the unbounded version had, kept deliberately.
    fastest_ids = [
        curve.publication_id
        for curve in sorted(
            (c for c in prefixes if c.views_within(early) is not None),
            key=lambda c: c.views_within(early) or 0,
            reverse=True,
        )[:_FASTEST_SHOWN]
    ]

    facts = _series_facts(db, prefixes, window_hours=window)
    verdicts = {
        curve.publication_id: _stall_verdict(
            curve,
            facts.get(curve.publication_id),
            window_hours=window,
            ratio=ratio,
        )
        for curve in prefixes
    }

    unsettled = [pid for pid, verdict in verdicts.items() if verdict is None]
    for curve in curves(db, user_id, publication_ids=unsettled, now=moment):
        verdicts[curve.publication_id] = curve.is_stalled(
            window_hours=window, ratio=ratio
        )

    shown = {
        curve.publication_id: curve
        for curve in prefixes
        if curve.publication_id in fastest_ids
        or verdicts[curve.publication_id]
    }
    for publication_id, curve in shown.items():
        # The prefix curve is now allowed to describe itself: everything it
        # could not see has been fetched, and this is where it is handed over.
        # A publication with no snapshots at all has no facts row, and its
        # lifetime is the zeroes `as_dict` used to read off an empty `points`.
        found = facts.get(publication_id)
        curve.lifetime = Lifetime(
            snapshots=found.snapshots if found else 0,
            views=found.total_views if found else 0,
            engagement=found.total_engagement if found else 0,
            stalled=bool(verdicts[publication_id]),
        )

    stalled_ids = [
        curve.publication_id for curve in prefixes if verdicts[curve.publication_id]
    ][:_STALLED_SHOWN]

    return {
        **shape,
        "publications": len(prefixes),
        "benchmarks": [b.as_dict() for b in benchmarks(db, user_id, known=prefixes)],
        "fastest": [shown[pid].as_dict() for pid in fastest_ids],
        "stalled": [shown[pid].as_dict() for pid in stalled_ids],
    }


__all__ = [
    "Benchmark",
    "Curve",
    "Lifetime",
    "Point",
    "benchmark_excluding",
    "benchmarks",
    "curves",
    "summary",
    "window_count_key",
]
