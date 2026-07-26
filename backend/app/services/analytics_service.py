"""Aggregations behind the analytics dashboard.

All of this is SQL over ``content_metrics``, which is append-only (see
:mod:`app.models.metrics`). Two consequences shape every query here:

* The *current* number for a publication is its latest snapshot, not a sum —
  summing an append-only series counts the same views once per poll.
* A metric a platform doesn't report stays ``NULL``, so averages use
  ``COUNT(field)`` rather than the row count. A LinkedIn post with no view
  count must not drag the average views per post toward zero.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus


@dataclass(frozen=True)
class Totals:
    content_count: int = 0
    published_count: int = 0
    publication_count: int = 0
    views: int = 0
    clicks: int = 0
    engagement: int = 0


def _latest_metric_subquery():
    """The id of the most recent metric row per publication.

    ``MAX(id)`` rather than ``MAX(captured_at)``: ids are monotonic and unique,
    so this can't tie, whereas two polls in the same second can.
    """
    return (
        select(func.max(ContentMetric.id).label("metric_id"))
        .group_by(ContentMetric.publication_id)
        .subquery()
    )


def _latest_metrics(db: Session, user_id: int) -> list[tuple[Publication, ContentMetric]]:
    """(publication, latest metric) for everything this user has published."""
    latest = _latest_metric_subquery()
    rows = db.execute(
        select(Publication, ContentMetric)
        .join(ContentMetric, ContentMetric.publication_id == Publication.id)
        .join(latest, latest.c.metric_id == ContentMetric.id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    ).all()
    return [(pub, metric) for pub, metric in rows]


def totals(db: Session, user_id: int, *, project_id: int | None = None) -> Totals:
    """Headline numbers for the dashboard."""
    content_q = (
        select(Content).join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
    )
    if project_id is not None:
        content_q = content_q.where(Content.project_id == project_id)

    all_content = list(db.scalars(content_q))
    content_ids = [c.id for c in all_content]

    published = sum(1 for c in all_content if c.status == ContentStatus.PUBLISHED)

    if not content_ids:
        return Totals()

    publications = list(
        db.scalars(
            select(Publication).where(
                Publication.content_id.in_(content_ids),
                Publication.status == PublicationStatus.PUBLISHED,
            )
        )
    )

    latest = _latest_metric_subquery()
    metrics = list(
        db.scalars(
            select(ContentMetric)
            .join(latest, latest.c.metric_id == ContentMetric.id)
            .where(ContentMetric.publication_id.in_([p.id for p in publications] or [0]))
        )
    )

    return Totals(
        content_count=len(all_content),
        published_count=published,
        publication_count=len(publications),
        views=sum(m.views or 0 for m in metrics),
        clicks=sum(m.clicks or 0 for m in metrics),
        engagement=sum(m.engagement for m in metrics),
    )


def by_content_type(db: Session, user_id: int) -> list[dict]:
    """Which content types actually perform, best first.

    Averages divide by the number of publications that *reported* a view count,
    not by the number that exist — see the module docstring.
    """
    buckets: dict[ContentType, dict] = defaultdict(
        lambda: {"published": 0, "views": 0, "engagement": 0, "with_views": 0}
    )

    latest = _latest_metric_subquery()
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
        bucket["engagement"] += metric.engagement
        if metric.views is not None:
            bucket["views"] += metric.views
            bucket["with_views"] += 1

    out = [
        {
            "content_type": ct.value,
            "label": ct.label,
            "publications": data["published"],
            "views": data["views"],
            "engagement": data["engagement"],
            "avg_views": round(data["views"] / data["with_views"], 1)
            if data["with_views"]
            else None,
        }
        for ct, data in buckets.items()
    ]
    return sorted(out, key=lambda d: d["views"], reverse=True)


def by_platform(db: Session, user_id: int) -> list[dict]:
    """Per-platform reach and reliability.

    Includes the failure count, because "LinkedIn gets no views" and "LinkedIn
    rejected every post" look identical in a views-only table and need very
    different responses.
    """
    stats: dict[Platform, dict] = defaultdict(
        lambda: {"published": 0, "failed": 0, "views": 0, "engagement": 0}
    )

    publications = list(
        db.scalars(
            select(Publication)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(Project.user_id == user_id)
        )
    )
    for publication in publications:
        bucket = stats[publication.platform]
        if publication.status == PublicationStatus.PUBLISHED:
            bucket["published"] += 1
        elif publication.status == PublicationStatus.FAILED:
            bucket["failed"] += 1

    for publication, metric in _latest_metrics(db, user_id):
        bucket = stats[publication.platform]
        bucket["views"] += metric.views or 0
        bucket["engagement"] += metric.engagement

    out = [
        {
            "platform": platform.value,
            "published": data["published"],
            "failed": data["failed"],
            "views": data["views"],
            "engagement": data["engagement"],
        }
        for platform, data in stats.items()
    ]
    return sorted(out, key=lambda d: d["views"], reverse=True)


def by_project(db: Session, user_id: int) -> list[dict]:
    """Which project is getting the traction."""
    projects = list(db.scalars(select(Project).where(Project.user_id == user_id)))
    index = {p.id: p for p in projects}

    stats: dict[int, dict] = {
        p.id: {"content": 0, "published": 0, "views": 0, "engagement": 0}
        for p in projects
    }

    for content in db.scalars(
        select(Content).where(Content.project_id.in_(list(index) or [0]))
    ):
        bucket = stats[content.project_id]
        bucket["content"] += 1
        if content.status == ContentStatus.PUBLISHED:
            bucket["published"] += 1

    latest = _latest_metric_subquery()
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


def top_content(db: Session, user_id: int, *, limit: int = 10) -> list[dict]:
    """The best-performing individual pieces."""
    scores: dict[int, dict] = defaultdict(lambda: {"views": 0, "engagement": 0})
    for publication, metric in _latest_metrics(db, user_id):
        bucket = scores[publication.content_id]
        bucket["views"] += metric.views or 0
        bucket["engagement"] += metric.engagement

    if not scores:
        return []

    content_rows = {
        c.id: c for c in db.scalars(select(Content).where(Content.id.in_(scores)))
    }
    out = [
        {
            "content_id": cid,
            "title": content_rows[cid].title,
            "content_type": content_rows[cid].content_type.value,
            "project_id": content_rows[cid].project_id,
            "published_at": content_rows[cid].published_at,
            **data,
        }
        for cid, data in scores.items()
        if cid in content_rows
    ]
    return sorted(out, key=lambda d: d["views"], reverse=True)[:limit]


def timeline(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """Publications per day over the last *days*, for the dashboard sparkline.

    Every day in the window is present, including the empty ones — a chart that
    silently omits zero days compresses a quiet fortnight into a flat line and
    makes it look like activity.
    """
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(Publication.published_at)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.published_at.is_not(None),
            Publication.published_at >= since,
        )
    ).all()

    counts: dict[str, int] = defaultdict(int)
    for (published_at,) in rows:
        counts[published_at.date().isoformat()] += 1

    start = since.date()
    return [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "publications": counts.get((start + timedelta(days=offset)).isoformat(), 0),
        }
        for offset in range(days + 1)
    ]


def overview(db: Session, user_id: int) -> dict:
    """Everything the analytics page needs, in one round trip."""
    return {
        "totals": totals(db, user_id).__dict__,
        "by_content_type": by_content_type(db, user_id),
        "by_platform": by_platform(db, user_id),
        "by_project": by_project(db, user_id),
        "top_content": top_content(db, user_id),
        "timeline": timeline(db, user_id),
    }


__all__ = [
    "Totals",
    "by_content_type",
    "by_platform",
    "by_project",
    "overview",
    "timeline",
    "top_content",
    "totals",
]
