"""Housekeeping tasks — things that must happen but not urgently.

Scheduled at low frequency (daily or less) and fine to skip during an outage.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete

from app.config import settings
from app.database import SessionLocal
from app.models.mixins import utcnow
from app.models.webhook import DeliveryStatus, WebhookDelivery
from app.services import password_reset
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
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


@celery_app.task(
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
