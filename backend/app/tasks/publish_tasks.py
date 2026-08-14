"""Publishing workers.

Every task here opens and closes its own session. A Celery task that borrows a
request's session is a use-after-close waiting to happen, and these can also be
called synchronously (see ``routers/content._dispatch``), where the caller's
session is still open and must not be touched.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models.content import Content, ContentStatus
from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.services import content_pipeline, publishing_service
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
        # Before the query, not after: a row abandoned mid-publish is re-armed
        # to `pending` and picked up by this same pass rather than waiting for
        # the next one.
        reclaimed = publishing_service.reclaim_stuck(db)
        due = publishing_service.due_publications(db)
        ids = [p.id for p in due]
    finally:
        db.close()

    dispatched = 0
    failed = 0
    inline = 0
    # One warning per sweep — see the same flag in
    # ``trigger_tasks.check_due_triggers``.
    broker_warned = False
    for publication_id in ids:
        try:
            publish_one.delay(publication_id)
        except Exception as exc:
            # Broker down — fall back to inline execution so the sweep does not
            # silently drop due publications.
            #
            # WARNING, not DEBUG, and carrying the exception: debug is off in
            # production, so the one branch that turns a beat sweep into a
            # serial inline publish of every due row logged nothing an operator
            # would ever see, and nothing about *why* the broker refused.
            if not broker_warned:
                logger.warning("broker unavailable, publishing inline: %s", exc)
                broker_warned = True
            inline += 1
            try:
                publish_one(publication_id)
            except Exception:
                # Same rule every other sweep states outright: one row's bad day
                # must not end the pass. This branch runs the publish *here*, so
                # anything `publish_one` does not catch lands in this loop —
                # `publishing_service.execute` promises never to raise, but the
                # work it does before its own `try` (resolving the adapter,
                # walking to the owner, the flush) is outside that promise.
                #
                # Escaping the loop would not lose one publication, it would
                # drop every later one in the batch, and the next pass selects
                # the same rows in the same order and dies in the same place.
                # An unpublishable row would take the whole publishing pipeline
                # down with it for as long as it sat there.
                logger.exception(
                    "inline publish of publication %s failed", publication_id
                )
                failed += 1
                continue
        dispatched += 1

    if ids:
        logger.info(
            "publish_due dispatched %d publication(s), %d failed, %d inline",
            dispatched,
            failed,
            inline,
        )
    # ``dispatched`` counts what was handed on, not what was selected. Reporting
    # ``len(ids)`` claimed credit for rows this pass had just dropped.
    return {"dispatched": dispatched, "failed": failed, "reclaimed": reclaimed}


@celery_app.task(
    name="app.tasks.publish_tasks.release_approved_content",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def release_approved_content() -> dict:
    """Beat task: queue approved pieces that nothing ever queued.

    ``publish_due`` sweeps the publications table, so a piece with no
    publication row is invisible to it however long it has been waiting — and
    approving one did not create a row until
    :func:`app.routers.content.approve_content` learned to. This is the backstop
    for everything that approved a piece another way: the rows already sitting
    ``approved`` before that fix shipped, an API client that PATCHes the status,
    a future endpoint that forgets.

    :func:`app.services.content_pipeline.release_approved` decides — it queues
    only for an active project on ``auto`` with destinations configured, and
    only for a piece that has no publications at all.
    """
    db = SessionLocal()
    try:
        stuck = list(
            db.scalars(
                select(Content)
                .join(Project, Project.id == Content.project_id)
                .join(User, User.id == Project.user_id)
                .outerjoin(Publication, Publication.content_id == Content.id)
                .where(
                    Content.status == ContentStatus.APPROVED,
                    Project.is_active.is_(True),
                    User.is_active.is_(True),
                    Project.autopilot_mode == AutopilotMode.AUTO,
                    Publication.id.is_(None),
                )
                # ``release_approved`` re-reads ``content.project`` and
                # ``project.user`` to re-check what the WHERE clause above
                # already filtered on, and both hops were lazy: two SELECTs per
                # stuck piece. The join that finds the rows is already visiting
                # both tables, so loading them costs nothing extra.
                .options(joinedload(Content.project).joinedload(Project.user))
            )
        )
        released = 0
        for content in stuck:
            try:
                if content_pipeline.release_approved(db, content):
                    released += 1
            except SoftTimeLimitExceeded:
                # Must precede the blanket handler: the soft limit arrives *as*
                # an Exception, so catching it as one piece's failure kept the
                # sweep running until the hard limit killed the worker mid
                # transaction. Stop and let the next pass take the remainder.
                logger.warning(
                    "release_approved_content timed out after %d of %d piece(s)",
                    released,
                    len(stuck),
                )
                break
            except Exception:
                # One unpublishable piece must not stop the sweep reaching the
                # rest — the row keeps its status and comes back next pass.
                logger.exception("could not release approved content %s", content.id)
                db.rollback()
    finally:
        db.close()

    if released:
        logger.info("released %d approved piece(s) that had nothing queued", released)
    return {"found": len(stuck), "released": released}


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
