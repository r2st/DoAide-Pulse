"""Housekeeping tasks — things that must happen but not urgently.

Scheduled at low frequency (daily or less) and fine to skip during an outage.
"""
from __future__ import annotations

import logging

from app.database import SessionLocal
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
