"""Housekeeping tasks — things that must happen but not urgently.

Scheduled at low frequency (daily or less) and fine to skip during an outage.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, func, or_

from app.config import settings
from app.database import SessionLocal
from app.models.mixins import utcnow
from app.models.preview_link import PreviewLink
from app.models.trigger import TriggerEvent, TriggerEventStatus
from app.models.webhook import DeliveryStatus, WebhookDelivery
from app.services import credential_rotation, password_reset
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
    name="app.tasks.maintenance_tasks.purge_expired_tokens",
    soft_time_limit=30,
    time_limit=60,
)
def purge_expired_tokens() -> dict:
    """Delete spent and expired password reset tokens.

    A single-user install accumulates a handful a year, but cleaning up is free
    and keeps the table honest.
    """
    db = SessionLocal()
    try:
        count = password_reset.purge_expired(db)
        if count:
            logger.info("purged %d expired password reset token(s)", count)
        return {"purged": count}
    finally:
        db.close()


@task(
    name="app.tasks.maintenance_tasks.rewrap_credentials",
    soft_time_limit=120,
    time_limit=180,
)
def rewrap_credentials() -> dict:
    """Move stored secrets onto the current ``TOKEN_ENCRYPTION_KEY``.

    A no-op on every install that has not rotated its key, which is the usual
    state — the sweep asks one cheap question per stored row and writes nothing.
    It earns its place on the schedule the day somebody does rotate: prepending
    a new key keeps everything readable, and this is what makes the *old* key
    removable afterwards, without which a rotation is only ever half done.

    Unlike the purge tasks either side of it, this one is worth running by hand
    straight after a rotation rather than waiting for the next daily tick:

        celery -A app.tasks.celery_app call \\
            app.tasks.maintenance_tasks.rewrap_credentials
    """
    db = SessionLocal()
    try:
        return credential_rotation.rewrap_all(db).as_dict()
    finally:
        db.close()


@task(
    name="app.tasks.maintenance_tasks.purge_old_webhook_deliveries",
    soft_time_limit=60,
    time_limit=120,
)
def purge_old_webhook_deliveries() -> dict:
    """Drop settled webhook deliveries older than the retention window.

    Only settled ones: a ``pending`` row is still owed an attempt no matter how
    old it looks, and deleting it would make a queue that quietly loses events
    under load. Each row carries a full payload, so an active install with
    several endpoints writes a few thousand a month — worth keeping long enough
    to answer "why didn't Slack hear about Tuesday's post", and not longer.
    """
    cutoff = utcnow() - timedelta(days=settings.webhook_delivery_retention_days)
    db = SessionLocal()
    try:
        result = db.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.status.in_(
                    [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED]
                ),
                WebhookDelivery.created_at < cutoff,
            )
        )
        db.commit()
        count = result.rowcount or 0
        if count:
            logger.info("purged %d settled webhook delivery row(s)", count)
        return {"purged": count}
    finally:
        db.close()


@task(
    name="app.tasks.maintenance_tasks.purge_old_trigger_events",
    soft_time_limit=60,
    time_limit=120,
)
def purge_old_trigger_events() -> dict:
    """Drop settled trigger events older than the retention window.

    Only settled ones. A ``received`` row is a firing whose outcome was never
    recorded — a worker died mid-generation — and that is exactly the row worth
    keeping until somebody has looked at it.

    Which somebody now can: ``triggers.reclaim_stuck_events``, run at the head
    of every trigger sweep, adopts the piece such a firing wrote or marks it
    failed. So a row reaching this query in ``received`` is one abandoned inside
    the last sweep interval and far too young for the cutoff, and the rows this
    deletes are settled ones carrying a stated reason — which is the state a
    firing should be in before it is thrown away.
    """
    cutoff = utcnow() - timedelta(days=settings.trigger_event_retention_days)
    db = SessionLocal()
    try:
        result = db.execute(
            delete(TriggerEvent).where(
                TriggerEvent.status.in_(
                    [
                        TriggerEventStatus.GENERATED,
                        TriggerEventStatus.SKIPPED,
                        TriggerEventStatus.FAILED,
                    ]
                ),
                TriggerEvent.created_at < cutoff,
            )
        )
        db.commit()
        count = result.rowcount or 0
        if count:
            logger.info("purged %d settled trigger event row(s)", count)
        return {"purged": count}
    finally:
        db.close()


@task(
    name="app.tasks.maintenance_tasks.purge_old_preview_links",
    soft_time_limit=60,
    time_limit=120,
)
def purge_old_preview_links() -> dict:
    """Drop dead preview links past the retention window.

    The other two sweeps here keep a debugging trail from getting expensive.
    This one keeps a *read* from getting expensive: ``list_for_content`` returns
    every link ever issued for a draft, and a reviewer cycle of issue-then-
    revoke adds a row each time with nothing ever taking one away.

    Dead means revoked or lapsed, and the cutoff is measured from whichever
    happened. A live link is never touched however old the row is — a
    thirty-day link issued twenty-nine days ago is still the URL somebody has
    in their inbox.
    """
    cutoff = utcnow() - timedelta(days=settings.preview_link_retention_days)
    db = SessionLocal()
    try:
        result = db.execute(
            delete(PreviewLink).where(
                or_(
                    PreviewLink.revoked_at.is_not(None),
                    PreviewLink.expires_at < utcnow(),
                ),
                func.coalesce(PreviewLink.revoked_at, PreviewLink.expires_at) < cutoff,
            )
        )
        db.commit()
        count = result.rowcount or 0
        if count:
            logger.info("purged %d dead preview link row(s)", count)
        return {"purged": count}
    finally:
        db.close()
