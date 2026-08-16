"""One piece's engagement, added up across everywhere it went.

Herald already collects this and already stores it. :mod:`app.tasks.metrics_tasks`
polls each live publication, :class:`app.models.metrics.ContentMetric` keeps the
series append-only, and ``/api/v1/analytics/*`` reads it back — but every one of
those reads is organised by *publication*: one platform's numbers, or one
account's, or one growth curve. The question a person asks about a piece they
wrote — "how did this post do?" — had no endpoint, because a piece is not a
publication. It is three of them, on three platforms, with three different
sets of fields reported.

**Adding up across platforms is the whole of the difficulty, and it is not
arithmetic.** Dev.to reports views, reactions and comments; Bluesky reports
reactions, replies and reposts and no views at all. Summing those into one
"views" number would report a piece's Dev.to views as its readership and quietly
imply Bluesky contributed nothing. So the totals here carry their own provenance:
:attr:`Totals.views_from` names the platforms that actually reported a view
count, and a field no platform reported stays ``None`` rather than becoming a
zero that drags an average down — the same rule
:class:`app.services.publishers.base.MetricsSnapshot` follows one layer up.

**The latest reading per platform, and the series behind it.** The snapshot
answers "where is it now"; the series answers "is it still growing", which is
the question that decides whether a post is worth another push. The series is
:mod:`app.services.velocity`'s, not a second implementation of it: the monotonic
clamp on a counter that goes backwards is load-bearing and subtle, and two
spellings of it would eventually disagree about the same post.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.metrics import ContentMetric
from app.models.publication import Platform, PublicationStatus
from app.services import velocity

logger = logging.getLogger(__name__)

#: The engagement fields a platform may report. Ordered as the UI shows them.
FIELDS = ("views", "reads", "clicks", "reactions", "comments", "shares")


@dataclass
class PlatformEngagement:
    """The newest reading for one publication, plus where the post lives."""

    publication_id: int
    platform: Platform
    external_url: str | None
    published_at: datetime | None
    #: When this reading was taken. ``None`` when the platform has never been
    #: polled — a post published four minutes ago, or a platform with no stats
    #: API at all. Distinct from "polled and reported nothing".
    captured_at: datetime | None = None
    snapshots: int = 0
    views: int | None = None
    reads: int | None = None
    clicks: int | None = None
    reactions: int | None = None
    comments: int | None = None
    shares: int | None = None

    @property
    def engagement(self) -> int:
        """Every interaction this platform reported, missing counted as zero.

        The same definition :attr:`app.models.metrics.ContentMetric.engagement`
        uses, deliberately — a piece's engagement must not be able to disagree
        with the sum of its publications'.
        """
        return (
            (self.reactions or 0)
            + (self.comments or 0)
            + (self.clicks or 0)
            + (self.shares or 0)
        )

    def as_dict(self) -> dict[str, Any]:
        """This platform's reading as JSON, with unreported counters left ``None``.

        The optional fields go out verbatim rather than coalesced to zero: the
        difference between "Bluesky does not report views" and "this post has
        no views" is the whole reason they are nullable.
        """
        return {
            "publication_id": self.publication_id,
            "platform": self.platform.value,
            "external_url": self.external_url,
            "published_at": self.published_at,
            "captured_at": self.captured_at,
            "snapshots": self.snapshots,
            "engagement": self.engagement,
            **{name: getattr(self, name) for name in FIELDS},
        }


@dataclass
class Totals:
    """The piece's numbers, added up, with the provenance of each one.

    A total is only meaningful alongside who reported it. Two reactions from
    Bluesky and none from a Dev.to post that was never polled is "2 reactions,
    from bluesky" — not "2 reactions" flat, which reads as though Dev.to
    reported zero.
    """

    views: int | None = None
    reads: int | None = None
    clicks: int | None = None
    reactions: int | None = None
    comments: int | None = None
    shares: int | None = None
    #: Which platforms contributed to each field, keyed by field name. A field
    #: nothing reported is absent here and ``None`` above.
    reported_by: dict[str, list[str]] = field(default_factory=dict)

    @property
    def engagement(self) -> int:
        """Every interaction anywhere, added up, missing counted as zero.

        Adds the same four counters :attr:`PlatformEngagement.engagement` does,
        so the piece's engagement is the sum of its platforms' and cannot
        disagree with them.
        """
        return (
            (self.reactions or 0)
            + (self.comments or 0)
            + (self.clicks or 0)
            + (self.shares or 0)
        )

    def as_dict(self) -> dict[str, Any]:
        """The totals as JSON, each one shipped beside the platforms behind it."""
        return {
            "engagement": self.engagement,
            "reported_by": self.reported_by,
            **{name: getattr(self, name) for name in FIELDS},
        }


@dataclass
class ContentEngagement:
    """Everything known about how one piece performed."""

    content_id: int
    title: str
    platforms: list[PlatformEngagement] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    #: One point per reading, summed across platforms — see :func:`_trend`.
    trend: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The whole answer as JSON: per platform, added up, and over time."""
        return {
            "content_id": self.content_id,
            "title": self.title,
            "platforms": [p.as_dict() for p in self.platforms],
            "totals": self.totals.as_dict(),
            "trend": self.trend,
        }


def _latest_metrics(db: Session, publication_ids: list[int]) -> dict[int, ContentMetric]:
    """The newest snapshot per publication, in one query.

    ``DISTINCT ON`` would be the Postgres way and is not the SQLite way, and
    this runs on both. A window function is: it is the same plan on either, and
    the composite index on ``(publication_id, captured_at)`` serves the
    partition without a sort.
    """
    if not publication_ids:
        return {}

    ranked = (
        select(
            ContentMetric,
            func.row_number()
            .over(
                partition_by=ContentMetric.publication_id,
                order_by=(ContentMetric.captured_at.desc(), ContentMetric.id.desc()),
            )
            .label("rank"),
        )
        .where(ContentMetric.publication_id.in_(publication_ids))
        .subquery()
    )
    rows = db.scalars(
        select(ContentMetric)
        .join(ranked, ContentMetric.id == ranked.c.id)
        .where(ranked.c.rank == 1)
    )
    return {row.publication_id: row for row in rows}


def _snapshot_counts(db: Session, publication_ids: list[int]) -> dict[int, int]:
    """How many readings each publication has, so "never polled" is visible."""
    if not publication_ids:
        return {}
    rows = db.execute(
        select(ContentMetric.publication_id, func.count(ContentMetric.id))
        .where(ContentMetric.publication_id.in_(publication_ids))
        .group_by(ContentMetric.publication_id)
    ).all()
    return dict(rows)


def _totals(platforms: list[PlatformEngagement]) -> Totals:
    """Add the platforms up, keeping track of which ones answered.

    A field stays ``None`` unless at least one platform reported it. Summing
    ``None`` as zero is the mistake this exists to avoid: it turns "Bluesky does
    not report views" into "Bluesky got no views", and a piece that did well on
    Bluesky then reads as half a failure.
    """
    totals = Totals()
    for name in FIELDS:
        contributors = [
            p for p in platforms if getattr(p, name) is not None
        ]
        if not contributors:
            continue
        setattr(totals, name, sum(getattr(p, name) for p in contributors))
        totals.reported_by[name] = [p.platform.value for p in contributors]
    return totals


def _trend(curves: list[velocity.Curve]) -> list[dict[str, Any]]:
    """The piece's readings over time, summed across its platforms.

    Keyed on hours since *that publication's* own publish moment rather than on
    wall-clock time, because that is the axis the question lives on: a
    syndicated copy posted a day later should line up with hour 12 of the
    original, not sit twelve hours to the right of it. It is also the axis
    :mod:`app.services.velocity` already builds, and its points carry the
    monotonic clamp that raw snapshots do not.

    Readings from different platforms rarely land on the same hour, so each
    point carries the running total across every platform that had reported *by
    then* — the last known value for the others, not zero. Summing only the
    platforms that happened to be polled at that instant would make the line
    saw-tooth downwards every time one platform reported and another did not.
    """
    if not curves:
        return []

    stamps = sorted({round(p.hours, 2) for curve in curves for p in curve.points})
    latest: dict[int, tuple[int, int]] = {}
    out: list[dict[str, Any]] = []

    for hours in stamps:
        for curve in curves:
            due = [p for p in curve.points if round(p.hours, 2) <= hours]
            if due:
                latest[curve.publication_id] = (due[-1].views, due[-1].engagement)
        out.append(
            {
                "hours": hours,
                "views": sum(v for v, _ in latest.values()),
                "engagement": sum(e for _, e in latest.values()),
            }
        )
    return out


def for_content(db: Session, content: Content, *, user_id: int) -> ContentEngagement:
    """Everything known about how *content* performed, across every platform.

    Only the publications that actually went live are included. A ``failed`` or
    ``scheduled`` row has no post to measure, and listing it with a row of
    ``None`` would put the reason a piece has no numbers ("it never published")
    in the same shape as the reason a live one has none ("the platform reports
    nothing"), which are different facts about different problems.

    *user_id* is passed to :func:`app.services.velocity.curves`, which filters
    ownership in its own query. The caller has already established that this
    piece is the user's; this keeps the series read from being the one place
    that trusts that instead of checking.
    """
    live = [
        p
        for p in content.publications
        if p.status == PublicationStatus.PUBLISHED and p.published_at is not None
    ]
    ids = [p.id for p in live]

    latest = _latest_metrics(db, ids)
    counts = _snapshot_counts(db, ids)

    platforms: list[PlatformEngagement] = []
    for publication in sorted(live, key=lambda p: (p.published_at, p.id)):
        metric = latest.get(publication.id)
        platforms.append(
            PlatformEngagement(
                publication_id=publication.id,
                platform=publication.platform,
                external_url=publication.external_url,
                published_at=publication.published_at,
                captured_at=metric.captured_at if metric else None,
                snapshots=counts.get(publication.id, 0),
                **{
                    name: getattr(metric, name) if metric else None for name in FIELDS
                },
            )
        )

    return ContentEngagement(
        content_id=content.id,
        title=content.title,
        platforms=platforms,
        totals=_totals(platforms),
        trend=_trend(
            velocity.curves(db, user_id, publication_ids=ids) if ids else []
        ),
    )


__all__ = [
    "FIELDS",
    "ContentEngagement",
    "PlatformEngagement",
    "Totals",
    "for_content",
]
