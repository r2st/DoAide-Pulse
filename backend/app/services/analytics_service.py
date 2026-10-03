"""Aggregations behind the analytics dashboard.

All of this is SQL over ``content_metrics``, which is append-only (see
:mod:`app.models.metrics`). Two consequences shape every query here:

* The *current* number for a publication is its latest snapshot, not a sum —
  summing an append-only series counts the same views once per poll.
* A metric a platform doesn't report stays ``NULL``, so averages use
  ``COUNT(field)`` rather than the row count. A LinkedIn post with no view
  count must not drag the average views per post toward zero.

The same discipline governs every *rate* here. A rate with nothing in its
denominator is ``None``, never ``0.0``: "nobody clicked" and "this platform
does not report views" are different facts, and a dashboard that renders both
as 0% quietly tells the user their best channel is dead.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.models.content import (
    Content,
    ContentStatus,
    ContentType,
    read_minutes_for,
)
from app.models.llm_usage import LLMUsage
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus

#: Reading-time bands, in minutes, for "does length pay off here?". Bounds are
#: inclusive upper edges; the last band is open-ended. Three bands rather than
#: a histogram because the decision they inform is ternary — write shorter,
#: keep going, or write longer.
_LENGTH_BANDS: tuple[tuple[str, int | None], ...] = (
    ("short", 3),
    ("medium", 8),
    ("long", None),
)


def _rate(
    numerator: int | None, denominator: int | None, *, reported: int | None = None
) -> float | None:
    """*numerator* / *denominator* as a fraction, or ``None`` when unanswerable.

    Fractions rather than percentages throughout: a rate that is sometimes 0.04
    and sometimes 4.0 depending on which endpoint returned it is a formatting
    bug waiting to happen in the UI. Four decimal places keeps a click in ten
    thousand views visible.

    *reported* is how many publications supplied the numerator at all. Pass it
    for any metric a platform may simply not have — reads and clicks — so that
    "Mastodon does not count reads" comes back as ``None`` rather than a read
    rate of 0.0, which reads as an audience that opens every post and finishes
    none of them.
    """
    if not denominator or numerator is None:
        return None
    if reported is not None and reported <= 0:
        return None
    return round(numerator / denominator, 4)


@dataclass(frozen=True)
class Totals:
    content_count: int = 0
    published_count: int = 0
    publication_count: int = 0
    views: int = 0
    #: Platforms that distinguish "opened" from "read to the end" report this.
    #: Dev.to and Medium do; the social platforms have nothing to report.
    reads: int = 0
    clicks: int = 0
    engagement: int = 0
    #: How many publications reported reads / clicks at all. Zero means the
    #: corresponding rate is unknown rather than zero — see :func:`_rate`.
    reads_reported: int = 0
    clicks_reported: int = 0

    @property
    def click_through_rate(self) -> float | None:
        """Clicks per view — how often a reader followed the link out."""
        return _rate(self.clicks, self.views, reported=self.clicks_reported)

    @property
    def read_rate(self) -> float | None:
        """Reads per view — how often an opened post was actually read."""
        return _rate(self.reads, self.views, reported=self.reads_reported)

    @property
    def engagement_rate(self) -> float | None:
        """Any interaction per view."""
        return _rate(self.engagement, self.views)

    def to_dict(self) -> dict:
        """Counts plus the derived rates, for the API.

        The rates are properties, so ``dataclasses.asdict`` alone would drop
        them silently — which is exactly the kind of omission nobody notices
        until the dashboard has been missing a number for a week.
        """
        return {
            **dataclasses.asdict(self),
            "click_through_rate": self.click_through_rate,
            "read_rate": self.read_rate,
            "engagement_rate": self.engagement_rate,
        }


def _blank_metrics() -> dict[str, int]:
    """A zeroed bucket the aggregations accumulate snapshots into."""
    return {
        "views": 0,
        "reads": 0,
        "clicks": 0,
        "engagement": 0,
        "reads_reported": 0,
        "clicks_reported": 0,
    }


def _accumulate(bucket: dict[str, int], metric: ContentMetric) -> None:
    """Fold one snapshot into *bucket*, keeping track of what was reported."""
    bucket["views"] += metric.views or 0
    bucket["engagement"] += metric.engagement
    if metric.reads is not None:
        bucket["reads"] += metric.reads
        bucket["reads_reported"] += 1
    if metric.clicks is not None:
        bucket["clicks"] += metric.clicks
        bucket["clicks_reported"] += 1


def _rates(bucket: dict[str, int]) -> dict[str, float | None]:
    """The three derived rates for an accumulated bucket."""
    return {
        "click_through_rate": _rate(
            bucket["clicks"], bucket["views"], reported=bucket["clicks_reported"]
        ),
        "read_rate": _rate(
            bucket["reads"], bucket["views"], reported=bucket["reads_reported"]
        ),
        # Engagement is derived from four columns that coalesce to zero, so it
        # is always reported: a zero here really is "nobody interacted".
        "engagement_rate": _rate(bucket["engagement"], bucket["views"]),
    }


def _latest_metric_subquery(user_id: int) -> Subquery:
    """The id of the most recent metric row per publication, for one user.

    ``MAX(id)`` rather than ``MAX(captured_at)``: ids are monotonic and unique,
    so this can't tie, whereas two polls in the same second can.

    The ownership join belongs *inside* the subquery even though every caller
    joins it against rows already filtered to the same user. Without it the
    grouping runs over the whole of ``content_metrics`` — every account's
    snapshots, in a table nothing prunes — and only then gets thrown away by
    the outer join. That is the same answer at a cost set by the size of the
    deployment rather than the size of the dashboard being drawn, and
    :func:`overview` builds this subquery six times per page load. Restricting
    the input cannot change the result: the maximum id within a publication's
    group does not depend on which other publications are in the table.
    """
    return (
        select(func.max(ContentMetric.id).label("metric_id"))
        .join(Publication, Publication.id == ContentMetric.publication_id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .group_by(ContentMetric.publication_id)
        .subquery()
    )


def _latest_metrics(db: Session, user_id: int) -> list[tuple[Publication, ContentMetric]]:
    """(publication, latest metric) for everything this user has published."""
    latest = _latest_metric_subquery(user_id)
    return list(
        db.execute(
            select(Publication, ContentMetric)
            .join(ContentMetric, ContentMetric.publication_id == Publication.id)
            .join(latest, latest.c.metric_id == ContentMetric.id)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(Project.user_id == user_id)
        ).all()
    )


@dataclass(frozen=True)
class _OverviewRow:
    """One (publication, latest-metric) pair enriched with content metadata.

    Built once by :func:`_overview_metrics` and consumed by the sub-functions
    that :func:`overview` calls, so the expensive 4-table-join GROUP BY in
    :func:`_latest_metric_subquery` runs once per page load instead of six
    times.
    """

    publication: Publication
    metric: ContentMetric
    content_id: int
    content_type: ContentType
    project_id: int
    word_count: int


def _overview_metrics(db: Session, user_id: int) -> list[_OverviewRow]:
    """Everything ``overview()``'s sub-functions need, in one query.

    Each sub-function used to call :func:`_latest_metric_subquery` on its own
    — the same 4-table join and GROUP BY, six times per overview page load.
    This runs it once and attaches the three Content columns the sub-functions
    read from their own separate queries.
    """
    latest = _latest_metric_subquery(user_id)
    rows = db.execute(
        select(
            Publication,
            ContentMetric,
            Content.id.label("cid"),
            Content.content_type,
            Content.project_id,
            Content.word_count,
        )
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    ).all()
    return [
        _OverviewRow(
            publication=pub,
            metric=metric,
            content_id=cid,
            content_type=ct,
            project_id=pid,
            word_count=wc,
        )
        for pub, metric, cid, ct, pid, wc in rows
    ]


def _totals_from_cached(
    db: Session,
    user_id: int,
    cached: list[_OverviewRow],
) -> Totals:
    """Derive :func:`totals` from a pre-fetched metrics set, no subquery."""
    base = (
        select(Content.id, Content.status)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    )
    content_sub = base.subquery()
    counts = db.execute(
        select(
            func.count().label("total"),
            func.count()
            .filter(content_sub.c.status == ContentStatus.PUBLISHED)
            .label("published"),
        ).select_from(content_sub)
    ).one()
    content_count = counts.total or 0
    published_count = counts.published or 0
    if content_count == 0:
        return Totals()

    pub_count = db.scalar(
        select(func.count())
        .select_from(Publication)
        .join(content_sub, content_sub.c.id == Publication.content_id)
        .where(Publication.status == PublicationStatus.PUBLISHED)
    ) or 0

    bucket = _blank_metrics()
    for row in cached:
        if row.publication.status == PublicationStatus.PUBLISHED:
            _accumulate(bucket, row.metric)
    return Totals(
        content_count=content_count,
        published_count=published_count,
        publication_count=pub_count,
        views=bucket["views"],
        reads=bucket["reads"],
        clicks=bucket["clicks"],
        engagement=bucket["engagement"],
        reads_reported=bucket["reads_reported"],
        clicks_reported=bucket["clicks_reported"],
    )


def totals(db: Session, user_id: int, *, project_id: int | None = None) -> Totals:
    """Headline numbers for the dashboard.

    Uses SQL aggregates instead of loading every row into Python.
    """
    base = (
        select(Content.id, Content.status)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    )
    if project_id is not None:
        base = base.where(Content.project_id == project_id)

    content_sub = base.subquery()
    counts = db.execute(
        select(
            func.count().label("total"),
            func.count()
            .filter(content_sub.c.status == ContentStatus.PUBLISHED)
            .label("published"),
        ).select_from(content_sub)
    ).one()
    content_count = counts.total or 0
    published_count = counts.published or 0

    if content_count == 0:
        return Totals()

    # Published publications for this user's content
    pub_count = db.scalar(
        select(func.count())
        .select_from(Publication)
        .join(content_sub, content_sub.c.id == Publication.content_id)
        .where(Publication.status == PublicationStatus.PUBLISHED)
    ) or 0

    # Aggregate latest metrics in SQL.
    # ``engagement`` is a Python property (reactions + comments + clicks + shares),
    # so we express it as a SQL expression here.
    _engagement_expr = (
        func.coalesce(ContentMetric.reactions, 0)
        + func.coalesce(ContentMetric.comments, 0)
        + func.coalesce(ContentMetric.clicks, 0)
        + func.coalesce(ContentMetric.shares, 0)
    )
    latest = _latest_metric_subquery(user_id)
    agg = db.execute(
        select(
            func.coalesce(func.sum(ContentMetric.views), 0),
            func.coalesce(func.sum(ContentMetric.reads), 0),
            func.coalesce(func.sum(ContentMetric.clicks), 0),
            func.coalesce(func.sum(_engagement_expr), 0),
            # COUNT skips NULLs, which is the whole point: it separates "no
            # reads" from "this platform has no such number".
            func.count(ContentMetric.reads),
            func.count(ContentMetric.clicks),
        )
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .join(Publication, Publication.id == ContentMetric.publication_id)
        .join(content_sub, content_sub.c.id == Publication.content_id)
        .where(Publication.status == PublicationStatus.PUBLISHED)
    ).one()

    return Totals(
        content_count=content_count,
        published_count=published_count,
        publication_count=pub_count,
        views=agg[0],
        reads=agg[1],
        clicks=agg[2],
        engagement=agg[3],
        reads_reported=agg[4],
        clicks_reported=agg[5],
    )


def _by_content_type_from_cached(cached: list[_OverviewRow]) -> list[dict]:
    """Derive :func:`by_content_type` from a pre-fetched metrics set."""
    buckets: dict[ContentType, dict] = defaultdict(
        lambda: {"published": 0, "with_views": 0, **_blank_metrics()}
    )
    for row in cached:
        bucket = buckets[row.content_type]
        bucket["published"] += 1
        _accumulate(bucket, row.metric)
        if row.metric.views is not None:
            bucket["with_views"] += 1
    return _format_content_type_buckets(buckets)


def by_content_type(db: Session, user_id: int) -> list[dict]:
    """Which content types actually perform, best first.

    Averages divide by the number of publications that *reported* a view count,
    not by the number that exist — see the module docstring.
    """
    buckets: dict[ContentType, dict] = defaultdict(
        lambda: {"published": 0, "with_views": 0, **_blank_metrics()}
    )

    latest = _latest_metric_subquery(user_id)
    rows = db.execute(
        select(Content.content_type, ContentMetric)
        .join(Publication, Publication.content_id == Content.id)
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    ).all()

    for content_type, metric in rows:
        bucket = buckets[content_type]
        bucket["published"] += 1
        _accumulate(bucket, metric)
        if metric.views is not None:
            bucket["with_views"] += 1

    return _format_content_type_buckets(buckets)


def _format_content_type_buckets(
    buckets: dict[ContentType, dict],
) -> list[dict]:
    out = [
        {
            "content_type": ct.value,
            "label": ct.label,
            "publications": data["published"],
            "views": data["views"],
            "reads": data["reads"],
            "clicks": data["clicks"],
            "engagement": data["engagement"],
            "avg_views": round(data["views"] / data["with_views"], 1)
            if data["with_views"]
            else None,
            **_rates(data),
        }
        for ct, data in buckets.items()
    ]
    return sorted(out, key=lambda d: d["views"], reverse=True)


def _by_platform_from_cached(
    db: Session, user_id: int, cached: list[_OverviewRow]
) -> list[dict]:
    """Derive :func:`by_platform` from a pre-fetched metrics set."""
    stats: dict[Platform, dict] = defaultdict(
        lambda: {"published": 0, "failed": 0, **_blank_metrics()}
    )
    platform_counts = db.execute(
        select(
            Publication.platform,
            Publication.status,
            func.count().label("cnt"),
        )
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .group_by(Publication.platform, Publication.status)
    ).all()
    for platform, pub_status, cnt in platform_counts:
        bucket = stats[platform]
        if pub_status == PublicationStatus.PUBLISHED:
            bucket["published"] += cnt
        elif pub_status == PublicationStatus.FAILED:
            bucket["failed"] += cnt
    for row in cached:
        _accumulate(stats[row.publication.platform], row.metric)
    return _format_platform_stats(stats)


def by_platform(db: Session, user_id: int) -> list[dict]:
    """Per-platform reach and reliability.

    Includes the failure count, because "LinkedIn gets no views" and "LinkedIn
    rejected every post" look identical in a views-only table and need very
    different responses.
    """
    stats: dict[Platform, dict] = defaultdict(
        lambda: {"published": 0, "failed": 0, **_blank_metrics()}
    )

    # Aggregate publication counts per platform in SQL
    platform_counts = db.execute(
        select(
            Publication.platform,
            Publication.status,
            func.count().label("cnt"),
        )
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .group_by(Publication.platform, Publication.status)
    ).all()
    for platform, pub_status, cnt in platform_counts:
        bucket = stats[platform]
        if pub_status == PublicationStatus.PUBLISHED:
            bucket["published"] += cnt
        elif pub_status == PublicationStatus.FAILED:
            bucket["failed"] += cnt

    for publication, metric in _latest_metrics(db, user_id):
        _accumulate(stats[publication.platform], metric)

    return _format_platform_stats(stats)


def _format_platform_stats(stats: dict[Platform, dict]) -> list[dict]:
    out = [
        {
            "platform": platform.value,
            "published": data["published"],
            "failed": data["failed"],
            "views": data["views"],
            "reads": data["reads"],
            "clicks": data["clicks"],
            "engagement": data["engagement"],
            **_rates(data),
        }
        for platform, data in stats.items()
    ]
    return sorted(out, key=lambda d: d["views"], reverse=True)


def _by_project_from_cached(
    db: Session, user_id: int, cached: list[_OverviewRow]
) -> list[dict]:
    """Derive :func:`by_project` from a pre-fetched metrics set."""
    projects = list(db.scalars(select(Project).where(Project.user_id == user_id)))
    index = {p.id: p for p in projects}
    stats: dict[int, dict] = {
        p.id: {"content": 0, "published": 0, "views": 0, "engagement": 0}
        for p in projects
    }
    counted = db.execute(
        select(
            Content.project_id,
            func.count(Content.id),
            func.count(Content.id).filter(Content.status == ContentStatus.PUBLISHED),
        )
        .where(Content.project_id.in_(list(index) or [0]))
        .group_by(Content.project_id)
    ).all()
    for project_id, total, published in counted:
        stats[project_id]["content"] = total
        stats[project_id]["published"] = published
    for row in cached:
        if row.project_id in stats:
            stats[row.project_id]["views"] += row.metric.views or 0
            stats[row.project_id]["engagement"] += row.metric.engagement
    return _format_project_stats(stats, index)


def by_project(db: Session, user_id: int) -> list[dict]:
    """Which project is getting the traction."""
    projects = list(db.scalars(select(Project).where(Project.user_id == user_id)))
    index = {p.id: p for p in projects}

    stats: dict[int, dict] = {
        p.id: {"content": 0, "published": 0, "views": 0, "engagement": 0}
        for p in projects
    }

    # Counted in SQL rather than by loading every piece the account has ever
    # written and incrementing in Python. Nothing bounds that read — content is
    # the table this product exists to grow — and the two numbers it was
    # extracting are a GROUP BY, which is what ``projects._batch_counts`` does
    # for the same pair on the projects page.
    counted = db.execute(
        select(
            Content.project_id,
            func.count(Content.id),
            func.count(Content.id).filter(Content.status == ContentStatus.PUBLISHED),
        )
        .where(Content.project_id.in_(list(index) or [0]))
        .group_by(Content.project_id)
    ).all()
    for project_id, total, published in counted:
        stats[project_id]["content"] = total
        stats[project_id]["published"] = published

    latest = _latest_metric_subquery(user_id)
    rows = db.execute(
        select(Content.project_id, ContentMetric)
        .join(Publication, Publication.content_id == Content.id)
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .where(Content.project_id.in_(list(index) or [0]))
    ).all()
    for project_id, metric in rows:
        stats[project_id]["views"] += metric.views or 0
        stats[project_id]["engagement"] += metric.engagement

    return _format_project_stats(stats, index)


def _format_project_stats(
    stats: dict[int, dict], index: dict[int, Project]
) -> list[dict]:
    out = [
        {
            "project_id": pid,
            "name": index[pid].name,
            "slug": index[pid].slug,
            **data,
        }
        for pid, data in stats.items()
    ]
    return sorted(out, key=lambda d: (d["views"], d["published"]), reverse=True)


#: What "best-performing" may be measured by. ``views`` is the default because
#: every platform reports it; ``engagement`` is the sum of the interaction
#: counters and is the better measure where it is reported, which is not
#: everywhere — see :meth:`app.models.metrics.ContentMetric.engagement`.
TOP_CONTENT_SORTS = ("views", "engagement", "clicks", "reads")


def _top_content_from_cached(
    db: Session,
    cached: list[_OverviewRow],
    *,
    limit: int = 10,
    sort: str = "views",
) -> list[dict]:
    """Derive :func:`top_content` from a pre-fetched metrics set."""
    if sort not in TOP_CONTENT_SORTS:
        raise ValueError(
            f"cannot rank by {sort!r}; known: {', '.join(TOP_CONTENT_SORTS)}"
        )
    scores: dict[int, dict] = defaultdict(_blank_metrics)
    for row in cached:
        _accumulate(scores[row.content_id], row.metric)
    if not scores:
        return []
    content_rows = {
        r.id: r
        for r in db.execute(
            select(
                Content.id,
                Content.title,
                Content.content_type,
                Content.project_id,
                Content.published_at,
                Content.word_count,
            ).where(Content.id.in_(scores))
        )
    }
    return _format_top_content(scores, content_rows, limit=limit, sort=sort)


def top_content(
    db: Session, user_id: int, *, limit: int = 10, sort: str = "views"
) -> list[dict]:
    """The best-performing individual pieces, by *sort*.

    Sorting on a counter no platform in the account reports gives every piece
    the same zero and returns them in an arbitrary order rather than an empty
    list — the pieces are real and their views are real, it is the ranking that
    is meaningless. The counts come back either way so a reader can see the
    column is empty; ``_rates`` already distinguishes "nobody did it" from "no
    platform reports it" for the derived rates beside them.
    """
    if sort not in TOP_CONTENT_SORTS:
        raise ValueError(
            f"cannot rank by {sort!r}; known: {', '.join(TOP_CONTENT_SORTS)}"
        )
    scores: dict[int, dict] = defaultdict(_blank_metrics)
    for publication, metric in _latest_metrics(db, user_id):
        _accumulate(scores[publication.content_id], metric)

    if not scores:
        return []

    # Columns rather than entities, the way ``read_minutes_for``'s docstring
    # asks callers to: five fields are wanted, and loading ``Content`` to reach
    # them brings the JSON columns nothing here reads and fires the
    # ``lazy="selectin"`` load of every publication on every piece — which is
    # doubly wasted, since the publications are what `_latest_metrics` already
    # walked to build ``scores``.
    content_rows = {
        row.id: row
        for row in db.execute(
            select(
                Content.id,
                Content.title,
                Content.content_type,
                Content.project_id,
                Content.published_at,
                Content.word_count,
            ).where(Content.id.in_(scores))
        )
    }
    return _format_top_content(scores, content_rows, limit=limit, sort=sort)


def _format_top_content(
    scores: dict[int, dict],
    content_rows: dict,
    *,
    limit: int,
    sort: str,
) -> list[dict]:
    out = [
        {
            "content_id": cid,
            "title": content_rows[cid].title,
            "content_type": content_rows[cid].content_type.value,
            "project_id": content_rows[cid].project_id,
            "published_at": content_rows[cid].published_at,
            "read_minutes": read_minutes_for(content_rows[cid].word_count),
            **data,
            **_rates(data),
        }
        for cid, data in scores.items()
        if cid in content_rows
    ]
    return sorted(out, key=lambda d: d[sort] or 0, reverse=True)[:limit]


def utc_day(value: datetime) -> str:
    """Which UTC day a stored timestamp falls on, as the bucket key for it.

    ``value.date()`` is the date *in whatever zone the value carries*, and a
    ``timestamptz`` arrives in the database session's zone. Pulse pins that to
    UTC at connect time (:func:`app.database._connect_options`), which is the
    real fix; this is the same statement made where the buckets are built, so a
    reading cannot be filed under a day the labels do not contain.

    That is the failure worth naming: the labels below run from
    :func:`window_start` and are UTC by construction, so a reading shifted into
    the day after the last label does not move buckets — it matches none of
    them and leaves the chart entirely. A silent subtraction, on the one screen
    whose job is to be counted on.
    """
    return as_aware(value).astimezone(UTC).date().isoformat()


def window_start(days: int) -> datetime:
    """Midnight UTC on the first day a *days*-long daily chart draws.

    The window has to start where the first bucket starts, and ``utcnow() -
    timedelta(days=days)`` does not: it lands at the current time of day. Both
    daily series below label their first bucket ``since.date()`` and then filter
    on ``>= since``, so that bucket was drawn for a whole day and filled from
    the sliver of it after the current clock time. A dashboard loaded at 21:00
    charted the first day of the month from 21:00 onwards and reported the other
    twenty-one hours as nothing having happened — and because the number moved
    every time the page was opened, the shortfall read as a quiet day rather
    than as a bug.

    Flooring to midnight makes every bucket a whole UTC day except today's,
    which is partial by definition and is the one bucket a reader expects to be.
    The labels do not change (the date of ``now - days`` is the same either
    way), so this only ever adds the readings the first bucket was already
    claiming to count.

    UTC because that is the only clock Pulse stores anything in — see
    :mod:`app.services.cadence`; the frontend renders these dates as given.
    """
    return (utcnow() - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def timeline(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """Publications per day over the last *days*, for the dashboard sparkline.

    Every day in the window is present, including the empty ones — a chart that
    silently omits zero days compresses a quiet fortnight into a flat line and
    makes it look like activity. Every day is also *whole* — see
    :func:`window_start`.
    """
    since = window_start(days)
    day_col = func.date(Publication.published_at).label("day")
    rows = db.execute(
        select(day_col, func.count(Publication.id).label("cnt"))
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.published_at.is_not(None),
            Publication.published_at >= since,
        )
        .group_by(day_col)
    ).all()

    counts: dict[str, int] = {str(day): cnt for day, cnt in rows}

    start = since.date()
    return [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "publications": counts.get((start + timedelta(days=offset)).isoformat(), 0),
        }
        for offset in range(days + 1)
    ]


#: Where a week's bucket starts. ISO weeks begin on Monday, and the label a
#: reader is shown is that Monday's date — so "week of the 3rd" means the seven
#: days from the 3rd, not the seven ending there.
_WEEK_START_WEEKDAY = 0


def utc_week(value: datetime) -> str:
    """Which UTC week a stored timestamp falls in, labelled by its Monday.

    The weekly counterpart of :func:`utc_day`, and through the same
    :func:`as_aware` conversion for the same reason: a reading shifted by a
    stored offset does not move buckets, it matches none of them and leaves the
    chart.
    """
    day = as_aware(value).astimezone(UTC).date()
    return (day - timedelta(days=(day.weekday() - _WEEK_START_WEEKDAY) % 7)).isoformat()


def published_series(
    db: Session, user_id: int, *, days: int = 30, weekly: bool = False
) -> list[dict]:
    """Pieces published per day, or per week, over the last *days*.

    Counts **content**, not publications — the difference is the whole reason
    this sits beside :func:`timeline` rather than replacing it. A piece
    cross-posted to five platforms is five rows in ``Publication`` and one
    thing the author wrote; ``timeline`` answers "how much did Pulse send"
    (five) and this answers "how much did I publish" (one). Charting the first
    as the second makes a quiet week with a wide cross-post look like a busy
    one.

    Every bucket in the window is present including the empty ones, for the
    reason :func:`timeline` gives: a chart that omits its zero days compresses
    a quiet fortnight into a flat line and makes it look like activity.

    The weekly form's first and last buckets are partial — the window starts on
    whatever weekday it starts on. That is left as it is rather than trimmed:
    dropping them would silently change the range the caller asked for, and a
    reader comparing the last bar against the one before it is looking at a
    week in progress in either case.
    """
    since = window_start(days)
    if weekly:
        base = db.execute(
            select(Content.published_at)
            .join(Project, Project.id == Content.project_id)
            .where(
                Project.user_id == user_id,
                Content.status == ContentStatus.PUBLISHED,
                Content.published_at.is_not(None),
                Content.published_at >= since,
            )
        ).all()
        counts: dict[str, int] = defaultdict(int)
        for (published_at,) in base:
            counts[utc_week(published_at)] += 1
    else:
        day_col = func.date(Content.published_at).label("day")
        rows = db.execute(
            select(day_col, func.count(Content.id).label("cnt"))
            .join(Project, Project.id == Content.project_id)
            .where(
                Project.user_id == user_id,
                Content.status == ContentStatus.PUBLISHED,
                Content.published_at.is_not(None),
                Content.published_at >= since,
            )
            .group_by(day_col)
        ).all()
        counts = {str(day): cnt for day, cnt in rows}

    start = since.date()
    if weekly:
        first = start - timedelta(days=(start.weekday() - _WEEK_START_WEEKDAY) % 7)
        labels = []
        cursor = first
        last = utcnow().date()
        while cursor <= last:
            labels.append(cursor.isoformat())
            cursor += timedelta(days=7)
    else:
        labels = [(start + timedelta(days=n)).isoformat() for n in range(days + 1)]

    return [{"period": label, "published": counts.get(label, 0)} for label in labels]


def generation_cost_trend(db: Session, *, days: int = 30) -> list[dict]:
    """Token spend per day over the last *days*, with the calls behind it.

    **Install-wide, not per-account.** :class:`~app.models.llm_usage.LLMUsage`
    has no owner column — see :mod:`app.services.ops_metrics` for why — so this
    is the deployment's spend, and on a multi-account install every caller sees
    the same series. It lives here rather than on the metrics endpoint because
    the question it answers ("is generation getting more expensive") is a
    dashboard question, and the caveat travels with it into the schema.

    "Cost" is tokens. Pulse runs on free tiers where the scarce thing is
    quota rather than money, and there is no price table to multiply by — a
    currency figure here would be invented. ``calls`` sits beside the tokens
    because the two move independently: a day whose token count doubled because
    twice as much was written is a different fact from one that doubled because
    each generation got longer, and ``tokens_per_call`` is what separates them.

    Rows are counted in the day they were *written*, which for a completion is
    when it finished. Every bucket in the window is present.
    """
    since = window_start(days)
    day_col = func.date(LLMUsage.created_at).label("day")
    rows = db.execute(
        select(
            day_col,
            func.count(LLMUsage.id).label("calls"),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(day_col)
    ).all()

    tokens: dict[str, int] = {}
    calls: dict[str, int] = {}
    for day, day_calls, day_tokens in rows:
        key = str(day)
        calls[key] = day_calls
        tokens[key] = int(day_tokens)

    start = since.date()
    out = []
    for offset in range(days + 1):
        label = (start + timedelta(days=offset)).isoformat()
        day_calls = calls.get(label, 0)
        day_tokens = tokens.get(label, 0)
        out.append(
            {
                "date": label,
                "calls": day_calls,
                "total_tokens": day_tokens,
                # None, not zero, on a day nothing was generated: "each call
                # averaged no tokens" is the one reading that is certainly
                # wrong, and a chart would plot it as a collapse in cost.
                "tokens_per_call": (
                    round(day_tokens / day_calls, 1) if day_calls else None
                ),
            }
        )
    return out


#: The fields a daily gain is computed for. ``engagement`` is derived rather
#: than stored, so it is read off the metric's own property.
_TREND_FIELDS = ("views", "reads", "clicks", "engagement")


def _field_of(metric: ContentMetric, field_: str) -> int | None:
    """One trend field off a snapshot. ``engagement`` is a property, not a column."""
    if field_ == "engagement":
        return metric.engagement
    return getattr(metric, field_)


def _daily_gains(
    snapshots: list[ContentMetric], *, baseline: ContentMetric | None = None
) -> dict[str, dict[str, int]]:
    """One publication's series reduced to what it *gained* on each day.

    The counters are cumulative (see the module docstring), so a day's activity
    is a subtraction between the last reading of that day and the last reading
    before it — never a sum over the readings in between, which counts the same
    views once per poll.

    A counter that goes backwards is clamped to its previous value rather than
    recorded as a negative gain, for the reason
    :class:`app.services.velocity.Curve` gives: nothing the author did made
    those views un-happen, and a purge or a rescrape must not read as a day
    when the audience shrank.

    *baseline* is the last reading from **before** the window, and without it
    the first day inside the window would be handed the post's entire lifetime
    as that day's gain — which is how a chart of "the last 30 days" opens with
    a spike that is really eight months of accumulation.

    A day is absent from a field entirely when no reading that day reported it,
    which is what keeps "Mastodon counts no reads" out of the read rate rather
    than turning it into a read rate of zero.
    """
    opening: dict[str, int] = {}
    if baseline is not None:
        for field_ in _TREND_FIELDS:
            value = _field_of(baseline, field_)
            if value is not None:
                opening[field_] = int(value)

    # Where each field's cumulative counter stood when each observed day closed.
    running = dict(opening)
    closing: dict[str, dict[str, int]] = {}
    for metric in sorted(snapshots, key=lambda m: (m.captured_at, m.id)):
        day = utc_day(metric.captured_at)
        for field_ in _TREND_FIELDS:
            value = _field_of(metric, field_)
            if value is None:
                continue
            running[field_] = max(running.get(field_, 0), int(value))
            closing.setdefault(day, {})[field_] = running[field_]

    per_day: dict[str, dict[str, int]] = {}
    previous = dict(opening)
    for day in sorted(closing):
        gains: dict[str, int] = {}
        for field_, end in closing[day].items():
            gains[field_] = end - previous.get(field_, 0)
            previous[field_] = end
        per_day[day] = gains
    return per_day


def _pre_window_readings(
    db: Session, since: datetime, publication_ids: list[int]
) -> dict[int, ContentMetric]:
    """The last reading before *since*, per publication.

    The baseline every first-day-in-window gain is measured against. ``MAX(id)``
    rather than ``MAX(captured_at)`` for the reason
    :func:`_latest_metric_subquery` gives: ids are unique and monotonic, so this
    cannot tie.
    """
    if not publication_ids:
        return {}
    newest_before = (
        select(func.max(ContentMetric.id).label("metric_id"))
        .where(
            ContentMetric.publication_id.in_(publication_ids),
            ContentMetric.captured_at < since,
        )
        .group_by(ContentMetric.publication_id)
        .subquery()
    )
    rows = db.scalars(
        select(ContentMetric).join(
            newest_before, newest_before.c.metric_id == ContentMetric.id
        )
    )
    return {row.publication_id: row for row in rows}


def _read_minutes_by_publication(
    db: Session, user_id: int, since: datetime, content_of: dict[int, int]
) -> dict[int, int]:
    """Reading time per publication, counting each piece exactly once.

    *content_of* maps publication id to the content id behind it, which is what
    the caller already learned from its own rows. Several publications of the
    same piece — the normal case, one per platform — share one reading time, so
    the counts are fetched keyed by *content* id and then spread back across the
    publications.

    The ids come from a subquery repeating the caller's own predicate rather
    than from an ``IN`` over ``content_of.values()``. It selects the same set
    either way, but as a subquery the statement carries two bind parameters
    instead of one per piece, and Postgres refuses a statement with more than
    65535 of them.
    """
    if not content_of:
        return {}
    in_window = (
        select(Publication.content_id)
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id, ContentMetric.captured_at >= since)
        .distinct()
    )
    minutes_of_content = {
        content_id: read_minutes_for(words)
        for content_id, words in db.execute(
            select(Content.id, Content.word_count).where(Content.id.in_(in_window))
        )
    }
    return {
        publication_id: minutes_of_content[content_id]
        for publication_id, content_id in content_of.items()
    }


def engagement_trend(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """Views and engagement *gained* per day over the trailing window.

    Distinct from :func:`timeline`, which counts *publish events*: this tracks
    reader activity, including on posts that went out long before the window
    started. Every day is present, including zero days — see :func:`timeline`
    for why.

    The counters the platforms report are cumulative, so a day's number is the
    difference between where a publication's series ended that day and where it
    ended the day before — the same subtraction :mod:`app.services.velocity`
    makes, and for the same reason the module docstring gives at the top of this
    file. Summing the snapshots instead, which is what this did, multiplied
    every day by the poll frequency *and* re-counted each post's whole lifetime
    on every day it was polled: six-hourly polling turned one post with 1,000
    lifetime views into 4,000 views a day, every day, for ever.

    ``reader_minutes`` is each day's reads weighted by how long the piece they
    belong to takes to read — the same quantity :func:`read_time` reports for
    all time, resolved per day so the dashboard can chart it. Weighting has to
    happen here rather than in the UI: a day's reads span pieces of different
    lengths, and multiplying a daily total by an average reading time invents
    attention that was never paid to any particular post.

    The window starts at midnight UTC, so the first day charted is a whole day
    rather than the part of it after the current clock time — see
    :func:`window_start`. It hid more here than in :func:`timeline`, where a
    missing publish is at least a missing *row*: a first-day gain is measured
    against the last reading before the window, so an un-floored window took its
    baseline from a reading inside the day it was charting. The morning's gain
    was subtracted away as part of the baseline and appeared in no bucket at all.
    """
    since = window_start(days)
    rows = db.execute(
        # ``Publication.content_id`` — an integer already on the row being
        # joined through — rather than the body it points at. Reading time is
        # derived from the body, but this query returns a row per *snapshot*,
        # and the body is a whole article: selecting it here shipped one full
        # copy per poll per publication, which at the six-hourly default is
        # ~120 copies of each piece per publication over a 30-day window, to
        # compute one number per publication. The bodies are fetched once each,
        # keyed by content id, in ``_read_minutes_by_publication`` below.
        select(ContentMetric, Publication.content_id)
        .join(Publication, Publication.id == ContentMetric.publication_id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id, ContentMetric.captured_at >= since)
    ).all()

    # Group by publication first: a gain is only meaningful against the same
    # post's own previous reading.
    series: dict[int, list[ContentMetric]] = defaultdict(list)
    content_of: dict[int, int] = {}
    for metric, content_id in rows:
        series[metric.publication_id].append(metric)
        content_of[metric.publication_id] = content_id

    read_minutes = _read_minutes_by_publication(db, user_id, since, content_of)

    baselines = _pre_window_readings(db, since, list(series))

    by_day: dict[str, dict[str, int]] = defaultdict(_blank_metrics)
    reader_minutes: dict[str, int] = defaultdict(int)
    for publication_id, snapshots in series.items():
        gains_by_day = _daily_gains(
            snapshots, baseline=baselines.get(publication_id)
        )
        for day, gains in gains_by_day.items():
            bucket = by_day[day]
            bucket["views"] += gains.get("views") or 0
            bucket["engagement"] += gains.get("engagement") or 0
            if "reads" in gains:
                bucket["reads"] += gains["reads"]
                bucket["reads_reported"] += 1
                # Reads, not views — see :func:`read_time` for why a bounce
                # contributes nothing here.
                reader_minutes[day] += gains["reads"] * read_minutes[publication_id]
            if "clicks" in gains:
                bucket["clicks"] += gains["clicks"]
                bucket["clicks_reported"] += 1

    start = since.date()
    out: list[dict] = []
    for offset in range(days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        bucket = by_day.get(day)
        if bucket is None:
            out.append(
                {
                    "date": day,
                    "views": 0,
                    "reads": 0,
                    "clicks": 0,
                    "engagement": 0,
                    "reader_minutes": 0,
                    "click_through_rate": None,
                    "read_rate": None,
                    "engagement_rate": None,
                }
            )
            continue
        out.append(
            {
                "date": day,
                "views": bucket["views"],
                "reads": bucket["reads"],
                "clicks": bucket["clicks"],
                "engagement": bucket["engagement"],
                "reader_minutes": reader_minutes[day],
                **_rates(bucket),
            }
        )
    return out


def _band_for(read_minutes: int) -> str:
    for name, upper in _LENGTH_BANDS:
        if upper is None or read_minutes <= upper:
            return name
    return _LENGTH_BANDS[-1][0]  # pragma: no cover - the last band is open-ended


def _read_time_from_cached(
    db: Session, user_id: int, cached: list[_OverviewRow]
) -> dict:
    """Derive :func:`read_time` from a pre-fetched metrics set."""
    counts = db.execute(
        select(Content.id, Content.word_count, Content.status)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            or_(
                Content.status == ContentStatus.PUBLISHED,
                Content.publications.any(),
            ),
        )
    ).all()
    published = [
        words for _, words, st in counts if st == ContentStatus.PUBLISHED
    ]
    minutes_of_content = {cid: read_minutes_for(words) for cid, words, _ in counts}
    return _read_time_aggregate(
        published,
        minutes_of_content,
        [(row.content_id, row.metric) for row in cached],
    )


def read_time(db: Session, user_id: int) -> dict:
    """How long this user's posts are, and whether the long ones pay off.

    Two questions, one query. The first is descriptive — how much reading has
    Pulse actually published. The second is the one worth acting on: a piece's
    reading time is known before it goes out, so if the twelve-minute tutorials
    consistently out-earn the two-minute announcements per view, that is a
    commissioning decision rather than a post-hoc observation.

    ``reader_minutes`` multiplies *reads* by reading time, not views: a view is
    somebody arriving, and counting the full reading time for a bounce would
    invent attention nobody paid. Publications on platforms that don't report
    reads contribute nothing to it, which is why the basis is reported
    alongside — a zero here often means "nowhere you publish counts reads",
    not "nobody read it".
    """
    # ``word_count`` rather than the whole entity, in both queries below.
    # Reading time and total words are both derived from the count, but
    # ``Content`` carries ``lazy="selectin"`` publications, so selecting the
    # entity pulled every publication of every published piece into memory as
    # well — for two sums and an average. Neither read is bounded by anything
    # except how much the account has written, which is what makes this the
    # widest read in the module: it has no ``limit`` and no window.
    #
    # One read of the counts rather than two, keyed by content id. The second
    # query below has a row per *publication*, so counting there billed a
    # syndicated piece once per platform it went out on — for a reading time
    # that is a property of the piece, identical on every one of them. The
    # ``or_`` is what lets the two share this: a piece can hold a publication
    # without still being ``PUBLISHED`` — a status walked back after the fact —
    # and the band loop below needs a reading time for it either way.
    counts = db.execute(
        select(Content.id, Content.word_count, Content.status)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            or_(
                Content.status == ContentStatus.PUBLISHED,
                Content.publications.any(),
            ),
        )
    ).all()
    published = [
        words for _, words, status in counts if status == ContentStatus.PUBLISHED
    ]
    minutes_of_content = {cid: read_minutes_for(words) for cid, words, _ in counts}

    latest = _latest_metric_subquery(user_id)
    rows = db.execute(
        select(Content.id, ContentMetric)
        .join(Publication, Publication.content_id == Content.id)
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    ).all()

    return _read_time_aggregate(published, minutes_of_content, list(rows))


def _read_time_aggregate(
    published: list[int],
    minutes_of_content: dict[int, int],
    rows: list[tuple[int, ContentMetric]],
) -> dict:
    bands: dict[str, dict] = {
        name: {
            "publications": 0,
            "read_minutes": 0,
            "reader_minutes": 0,
            **_blank_metrics(),
        }
        for name, _ in _LENGTH_BANDS
    }
    reader_minutes = 0
    total_views = total_reads = 0
    reads_reported = 0

    for content_id, metric in rows:
        minutes = minutes_of_content.get(content_id, 1)
        bucket = bands[_band_for(minutes)]
        bucket["publications"] += 1
        bucket["read_minutes"] += minutes
        _accumulate(bucket, metric)

        total_views += metric.views or 0
        if metric.reads is not None:
            total_reads += metric.reads
            reads_reported += 1
            reader_minutes += metric.reads * minutes
            bucket["reader_minutes"] += metric.reads * minutes

    return {
        "published_pieces": len(published),
        "avg_read_minutes": round(
            sum(read_minutes_for(words) for words in published) / len(published), 1
        )
        if published
        else None,
        "total_words": sum(published),
        "reader_minutes": reader_minutes,
        "publications_reporting_reads": reads_reported,
        "read_rate": _rate(total_reads, total_views) if reads_reported else None,
        "by_length": [
            {
                "band": name,
                "max_read_minutes": upper,
                "publications": bands[name]["publications"],
                "avg_read_minutes": round(
                    bands[name]["read_minutes"] / bands[name]["publications"], 1
                )
                if bands[name]["publications"]
                else None,
                "views": bands[name]["views"],
                "reads": bands[name]["reads"],
                "clicks": bands[name]["clicks"],
                "engagement": bands[name]["engagement"],
                "reader_minutes": bands[name]["reader_minutes"],
                "avg_views": round(
                    bands[name]["views"] / bands[name]["publications"], 1
                )
                if bands[name]["publications"]
                else None,
                **_rates(bands[name]),
            }
            for name, upper in _LENGTH_BANDS
        ],
    }


def overview(db: Session, user_id: int) -> dict:
    """Everything the analytics page needs, in one round trip.

    Fetches the latest-metric-per-publication set once and passes it to the
    five sub-functions that used to compute it independently — six executions
    of the same 4-table-join GROUP BY replaced by one.  ``timeline`` and
    ``engagement_trend`` use different queries (windowed, not latest-only)
    and are unaffected.
    """
    cached = _overview_metrics(db, user_id)
    return {
        "totals": _totals_from_cached(db, user_id, cached).to_dict(),
        "by_content_type": _by_content_type_from_cached(cached),
        "by_platform": _by_platform_from_cached(db, user_id, cached),
        "by_project": _by_project_from_cached(db, user_id, cached),
        "top_content": _top_content_from_cached(db, cached),
        "timeline": timeline(db, user_id),
        "engagement_trend": engagement_trend(db, user_id),
        "read_time": _read_time_from_cached(db, user_id, cached),
    }


def dashboard_summary(
    db: Session, user_id: int
) -> tuple[Totals, list[dict]]:
    """Totals and per-project stats for the dashboard, one metric query.

    The dashboard needs both :func:`totals` and :func:`by_project`, and each
    runs the 4-table GROUP BY in :func:`_latest_metric_subquery` on its own.
    This fetches the set once and derives both, the same pattern
    :func:`overview` uses for the analytics page.
    """
    cached = _overview_metrics(db, user_id)
    return (
        _totals_from_cached(db, user_id, cached),
        _by_project_from_cached(db, user_id, cached),
    )


__all__ = [
    "TOP_CONTENT_SORTS",
    "Totals",
    "by_content_type",
    "by_platform",
    "by_project",
    "dashboard_summary",
    "engagement_trend",
    "generation_cost_trend",
    "overview",
    "published_series",
    "read_time",
    "timeline",
    "top_content",
    "totals",
    "utc_week",
]
