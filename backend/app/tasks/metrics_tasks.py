"""Poll published posts for engagement numbers."""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.content import Content
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.services import publishers, publishing_service
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
    name="app.tasks.metrics_tasks.collect_all_metrics",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def collect_all_metrics() -> dict:
    """Beat task: snapshot engagement for everything published.

    Only platforms whose adapter reports metrics are polled — see
    ``Adapter.supports_metrics``. Skipping the rest keeps this from making a
    round trip per post to a platform that has no stats API at all.
    """
    metric_platforms = [
        adapter.platform
        for adapter in publishers.all_adapters()
        if adapter.implemented and adapter.supports_metrics
    ]
    if not metric_platforms:
        return {"polled": 0, "recorded": 0}

    db = SessionLocal()
    try:
        # The owner comes back as a column off the join, and is handed to
        # ``collect_metrics`` rather than walked to.
        #
        # Eager-loading the two hops instead — which is what this did — was only
        # half a fix, and the half that showed up in a test where
        # ``collect_metrics`` is stubbed and never commits. In production it
        # records a metric per publication, and each commit expires the session:
        # the next iteration re-read the publication, then re-read its whole
        # ``Content`` (article body and all) and its project, one publication at
        # a time, exactly as if nothing had been eagerly loaded. Three SELECTs
        # per row on the sweep whose row count grows with everything the install
        # has ever published, one of them carrying an article.
        #
        # Not traversing the relationship at all is what survives the commits.
        # The publication itself is still re-read after each one — it is the row
        # being written about, and ``collect_metrics`` re-checks its status
        # before polling — but that is one narrow SELECT, not three wide ones.
        rows = db.execute(
            select(Publication, Project.user_id)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.platform.in_(metric_platforms),
                Publication.external_id.is_not(None),
            )
        ).all()
        recorded = 0
        # Shared across the whole sweep: once a platform rate-limits one
        # account, the rest of that account's posts there are skipped without a
        # request. See ``publishing_service.collect_metrics``.
        rate_limited: set[publishing_service.RateLimitKey] = set()
        for publication, user_id in rows:
            try:
                if (
                    publishing_service.collect_metrics(
                        db, publication, user_id=user_id, rate_limited=rate_limited
                    )
                    is not None
                ):
                    recorded += 1
            except SoftTimeLimitExceeded:
                logger.warning(
                    "metrics collection timed out after %d of %d publications",
                    recorded, len(rows),
                )
                break
            except Exception:
                # Isolate failures: a broken response from one platform must not
                # prevent polling the rest.
                logger.exception(
                    "metrics collection failed for publication %s", publication.id
                )
    finally:
        db.close()

    logger.info("metrics: polled %d, recorded %d", len(rows), recorded)
    return {"polled": len(rows), "recorded": recorded}


@task(
    name="app.tasks.metrics_tasks.collect_one",
    soft_time_limit=60,
    time_limit=90,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
)
def collect_one(publication_id: int) -> dict:
    """Poll a single publication — used by the "refresh" button."""
    db = SessionLocal()
    try:
        publication = db.get(Publication, publication_id)
        if publication is None:
            return {"publication_id": publication_id, "recorded": False}
        metric = publishing_service.collect_metrics(db, publication)
        return {"publication_id": publication_id, "recorded": metric is not None}
    finally:
        db.close()
