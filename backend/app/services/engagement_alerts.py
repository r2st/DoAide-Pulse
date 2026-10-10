"""Noticing that a piece took off, and saying so once.

Pulse already collects engagement — :mod:`app.tasks.metrics_tasks` snapshots
every published post every few hours, and :mod:`app.services.content_engagement`
adds the snapshots up for the dashboard. What none of that does is *tell*
anybody. The numbers sit there until somebody opens the app, which means the one
piece in fifty that is actually working gets found a week late, if at all.

This is the missing half: a per-project threshold, and one webhook when a piece
crosses it.

Three decisions carry the design.

**Once, ever.** The check is "engagement >= threshold", which becomes true and
then stays true. Run on every sweep with no guard, that is a webhook every few
hours for the rest of the piece's life — and a receiver that gets spammed by an
alert mutes the alert, not the piece.
:attr:`app.models.content.Content.engagement_notified_at` is the latch, written
in the same transaction as the emit.

**From the latest snapshot per publication.** ``content_metrics`` is append-only
(see :class:`app.models.metrics.ContentMetric`), so summing every row counts the
same interactions once per poll — a number that crosses any threshold eventually
regardless of whether anybody engaged with anything.

**Never raises.** This runs at the tail of the metrics sweep, and a sweep that
polled forty platforms successfully must not end in a traceback because one
project's notification could not be assembled. Same contract as
:func:`app.services.webhooks.emit`, for the same reason.

One more thing shapes the queries: no ``Content`` *entity* is loaded until a
piece has actually crossed. ``Content`` carries ``body_markdown``, so selecting
the ORM object for every candidate would put an article body per published piece
on a task that runs every few hours forever — the exact width
``test_metrics_sweep_query_budget`` exists to keep off this path. The candidate
pass reads three scalar columns; the entity is loaded only for the pieces being
announced, which need the whole row for the webhook body and are almost always
none.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.content import Content, ContentStatus
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.webhook import WebhookEvent
from app.services import webhook_payloads, webhooks

logger = logging.getLogger(__name__)


@dataclass
class Crossing:
    """One piece that just passed its project's threshold."""

    content_id: int
    threshold: int
    engagement: int
    views: int
    platforms: list[str]


def _latest_totals(db: Session, content_ids: list[int]) -> dict[int, tuple[int, int, list[str]]]:
    """``{content_id: (engagement, views, platforms)}`` from the newest snapshots.

    One pass for the whole batch rather than a query per piece: the sweep hands
    over everything it just polled, and a project publishing to four platforms
    would otherwise cost four round trips per piece per sweep.

    Only ``PUBLISHED`` publications count. A piece whose Dev.to copy failed is
    not credited with Dev.to's zero, and — more to the point — a copy that was
    later cancelled stops contributing rather than freezing its last reading
    into the total forever.
    """
    if not content_ids:
        return {}

    pub_rows = db.execute(
        select(Publication.id, Publication.content_id, Publication.platform).where(
            Publication.content_id.in_(content_ids),
            Publication.status == PublicationStatus.PUBLISHED,
        )
    ).all()
    if not pub_rows:
        return {}

    by_publication = {row[0]: (row[1], row[2].value) for row in pub_rows}

    # The newest capture per publication, then the row at that instant. Two
    # statements over an indexed column, independent of how many pieces are in
    # the batch.
    latest = (
        select(
            ContentMetric.publication_id.label("pub_id"),
            func.max(ContentMetric.captured_at).label("captured"),
        )
        .where(ContentMetric.publication_id.in_(list(by_publication)))
        .group_by(ContentMetric.publication_id)
        .subquery()
    )
    totals: dict[int, tuple[int, int, list[str]]] = {}
    seen_platforms: dict[int, set[str]] = {}
    for metric in db.scalars(
        select(ContentMetric).join(
            latest,
            (ContentMetric.publication_id == latest.c.pub_id)
            & (ContentMetric.captured_at == latest.c.captured),
        )
    ):
        content_id, platform = by_publication[metric.publication_id]
        engagement, views, _ = totals.get(content_id, (0, 0, []))
        totals[content_id] = (
            engagement + metric.engagement,
            views + (metric.views or 0),
            [],
        )
        seen_platforms.setdefault(content_id, set()).add(platform)

    return {
        content_id: (engagement, views, sorted(seen_platforms.get(content_id, ())))
        for content_id, (engagement, views, _) in totals.items()
    }


def evaluate(
    db: Session, content_ids: list[int], *, now: datetime | None = None
) -> list[Crossing]:
    """Fire ``content.engagement_threshold`` for any of *content_ids* that qualify.

    Returns the crossings, which is what the sweep logs and what the tests
    assert on. An empty list is the overwhelmingly common answer and costs one
    query when no project on the batch has a threshold set at all.

    Commits per crossing rather than once at the end: each is an independent
    notification, and one piece whose webhook fails to queue must not roll back
    the latch on a piece whose webhook went out fine.
    """
    if not content_ids:
        return []

    moment = now or utcnow()
    # Everything needed to *decide*, as four scalar columns rather than an ORM
    # entity: the piece's id, its project's threshold, and the owner the webhook
    # belongs to. Narrowed to pieces that could possibly qualify — published,
    # not already announced, and in a project that asked to be told — so an
    # install where nobody set a threshold pays one indexed query that returns
    # nothing.
    #
    # Deliberately not ``select(Content)``. That would carry ``body_markdown``
    # for every published piece the sweep just polled, every few hours, forever.
    candidates = db.execute(
        select(Content.id, Project.engagement_threshold, Project.user_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Content.id.in_(set(content_ids)),
            Content.status == ContentStatus.PUBLISHED,
            Content.engagement_notified_at.is_(None),
            Project.engagement_threshold > 0,
        )
    ).all()
    if not candidates:
        return []

    totals = _latest_totals(db, [row[0] for row in candidates])

    crossings: list[Crossing] = []
    for content_id, threshold, user_id in candidates:
        engagement, views, platforms = totals.get(content_id, (0, 0, []))
        if engagement < threshold:
            continue

        # Only now is the whole row worth reading: this piece is being
        # announced, and the webhook body is the piece.
        try:
            # Atomic conditional UPDATE: set the latch only if it is still
            # NULL, so two concurrent sweeps cannot both succeed. The
            # rowcount is the verdict — exactly one caller observes 1.
            result = db.execute(
                update(Content)
                .where(
                    Content.id == content_id,
                    Content.engagement_notified_at.is_(None),
                )
                .values(engagement_notified_at=moment)
            )
            db.commit()
            if result.rowcount == 0:
                continue
        except Exception:
            logger.exception(
                "failed to latch engagement notification for content %s "
                "(engagement=%d, threshold=%d)",
                content_id, engagement, threshold,
            )
            db.rollback()
            continue

        content = db.get(Content, content_id)
        if content is None:  # pragma: no cover - deleted mid-sweep
            continue

        webhooks.emit(
            db,
            user_id=user_id,
            event=WebhookEvent.ENGAGEMENT_THRESHOLD,
            data=webhook_payloads.engagement_payload(
                content,
                threshold=threshold,
                engagement=engagement,
                views=views,
                platforms=platforms,
            ),
        )
        logger.info(
            "content %s passed its engagement threshold (%d >= %d)",
            content_id, engagement, threshold,
        )
        crossings.append(
            Crossing(
                content_id=content_id,
                threshold=threshold,
                engagement=engagement,
                views=views,
                platforms=platforms,
            )
        )

    return crossings


__all__ = ["Crossing", "evaluate"]
