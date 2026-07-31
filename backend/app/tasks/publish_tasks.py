"""Publishing workers.

Every task here opens and closes its own session. A Celery task that borrows a
request's session is a use-after-close waiting to happen, and these can also be
called synchronously (see ``routers/content._dispatch``), where the caller's
session is still open and must not be touched.
"""
from __future__ import annotations

import logging

from app.database import SessionLocal
from app.models.publication import Publication, PublicationStatus
from app.services import publishing_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.publish_tasks.publish_one", soft_time_limit=120, time_limit=150)
def publish_one(publication_id: int) -> dict:
    """Publish one queued publication.

    Returns a small dict for the logs. Never raises — a failing publish records
    itself on the row (see :func:`app.services.publishing_service.execute`), and
    letting the exception escape would only add a Celery retry on top of the
    attempt counter we already keep.
    """
    db = SessionLocal()
    try:
        publication = db.get(Publication, publication_id)
        if publication is None:
            logger.warning("publish_one: publication %s is gone", publication_id)
            return {"publication_id": publication_id, "status": "missing"}
        if publication.is_terminal:
            return {
                "publication_id": publication_id,
                "status": publication.status.value,
                "skipped": True,
            }

        publishing_service.execute(db, publication)
        return {
            "publication_id": publication_id,
            "status": publication.status.value,
            "url": publication.external_url,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.publish_tasks.publish_due", soft_time_limit=300, time_limit=360)
def publish_due() -> dict:
    """Beat task: publish everything whose time has come.

    Includes rows left ``pending`` by a retryable failure, which is how the
    retry actually happens — there is no per-row retry timer, just this sweep
    picking them up again on the next pass.
    """
    db = SessionLocal()
    try:
        due = publishing_service.due_publications(db)
        ids = [p.id for p in due]
    finally:
        db.close()

    for publication_id in ids:
        try:
            publish_one.delay(publication_id)
        except Exception:
            # Broker down — fall back to inline execution so the sweep does not
            # silently drop due publications.
            logger.debug("broker unavailable, running publish_one inline")
            publish_one(publication_id)

    if ids:
        logger.info("publish_due dispatched %d publication(s)", len(ids))
    return {"dispatched": len(ids)}


@celery_app.task(name="app.tasks.publish_tasks.cancel_publication", soft_time_limit=30, time_limit=60)
def cancel_publication(publication_id: int) -> dict:
    """Stop a scheduled publication before it goes out."""
    db = SessionLocal()
    try:
        publication = db.get(Publication, publication_id)
        if publication is None or publication.is_terminal:
            return {"publication_id": publication_id, "cancelled": False}
        publication.status = PublicationStatus.CANCELLED
        db.commit()
        return {"publication_id": publication_id, "cancelled": True}
    finally:
        db.close()
