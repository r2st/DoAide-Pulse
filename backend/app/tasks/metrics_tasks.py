"""Poll published posts for engagement numbers."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.database import SessionLocal
from app.models.publication import Publication, PublicationStatus
from app.services import publishers, publishing_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.metrics_tasks.collect_all_metrics")
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
        publications = list(
            db.scalars(
                select(Publication).where(
                    Publication.status == PublicationStatus.PUBLISHED,
                    Publication.platform.in_(metric_platforms),
                    Publication.external_id.is_not(None),
                )
            )
        )
        recorded = 0
        for publication in publications:
            try:
                if publishing_service.collect_metrics(db, publication) is not None:
                    recorded += 1
            except Exception:
                # Isolate failures: a broken response from one platform must not
                # prevent polling the rest.
                logger.exception(
                    "metrics collection failed for publication %s", publication.id
                )
    finally:
        db.close()

    logger.info("metrics: polled %d, recorded %d", len(publications), recorded)
    return {"polled": len(publications), "recorded": recorded}


@celery_app.task(name="app.tasks.metrics_tasks.collect_one")
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
