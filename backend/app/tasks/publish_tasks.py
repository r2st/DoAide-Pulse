"""Publishing workers.

Every task here opens and closes its own session. A Celery task that borrows a
request's session is a use-after-close waiting to happen, and these can also be
called synchronously (see ``routers/content._dispatch``), where the caller's
session is still open and must not be touched.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.mixins import utcnow
from app.models.publication import Publication, PublicationStatus
from app.services import publishing_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.publish_tasks.publish_one",
    soft_time_limit=120,
    time_limit=150,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def publish_one(publication_id: int) -> dict:
    """Publish one queued publication.

    Returns a small dict for the logs. Never raises — a failing publish records
    itself on the row (see :func:`app.services.publishing_service.execute`), and
    letting the exception escape would only add a Celery retry on top of the
    attempt counter we already keep.
    """
    db = SessionLocal()
    try:
        # Atomic claim: only the first worker to transition the row from a
        # dispatchable state to PUBLISHING wins.  A concurrent publish_one
        # for the same id will see rowcount=0 and skip.
        #
        # ``scheduled_for`` is part of the claim, not just of the beat sweep's
        # query. A row parked in the future is not dispatchable however it got
        # here, and two callers hand this task ids that include such rows:
        # ``routers.content._queue_publish`` and
        # ``services.content_pipeline.generate_and_route`` both dispatch every
        # publication in a batch, and a cross-post batch contains the syndicated
        # copies that ``publishing_service._syndication_schedule`` deliberately
        # pushed behind the canonical. Claiming on status alone published those
        # copies in the same instant as the original — which is the exact
        # outcome the stagger exists to prevent, since a copy that goes out
        # before the original has a URL cannot carry a canonical link to it.
        # Rate-limited rows parked by ``publishing_service._defer`` were open to
        # the same early pickup.
        claimed = db.execute(
            update(Publication)
            .where(
                Publication.id == publication_id,
                Publication.status.in_(
                    [PublicationStatus.PENDING, PublicationStatus.SCHEDULED]
                ),
                (Publication.scheduled_for.is_(None))
                | (Publication.scheduled_for <= utcnow()),
            )
            .values(status=PublicationStatus.PUBLISHING)
            # The default, "evaluate", re-runs this WHERE clause in Python
            # against whatever is already in the identity map — and a datetime
            # comparison there raises rather than matching, because SQLite hands
            # back a naive value for a column ``utcnow()`` answers with an aware
            # one. Nothing needs synchronising: the commit below expires the
            # session and the row is read back with ``db.get``.
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()

        publication = db.get(Publication, publication_id)
        if publication is None:
            logger.warning("publish_one: publication %s is gone", publication_id)
            return {"publication_id": publication_id, "status": "missing"}
        if not claimed:
            # Another worker already claimed it, it is in a terminal state, or
            # its time has not come. The beat sweep comes back for the last.
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
    except SoftTimeLimitExceeded:
        logger.warning(
            "publish_one timed out for publication %s", publication_id
        )
        # Record the timeout so it shows up in the UI and can be retried.
        try:
            publication = db.get(Publication, publication_id)
            if publication and not publication.is_terminal:
                publication.error = "Task timed out — the platform may be slow"
                publication.status = PublicationStatus.PENDING
                db.commit()
        except Exception:
            logger.exception("failed to record timeout for publication %s", publication_id)
        return {"publication_id": publication_id, "status": "timeout"}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.publish_tasks.publish_due",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
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


@celery_app.task(
    name="app.tasks.publish_tasks.cancel_publication", soft_time_limit=30, time_limit=60
)
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
