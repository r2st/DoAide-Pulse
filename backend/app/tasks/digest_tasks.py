"""The weekly digest sweep.

Runs once a week rather than on a seconds interval, because "every 604800
seconds" drifts relative to the calendar and a summary that arrives at 03:12 on
a Thursday is a summary nobody opens.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.user import User
from app.services import digest, mailer
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
    name="app.tasks.digest_tasks.send_weekly_digests",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def send_weekly_digests() -> dict:
    """Beat task: mail every subscribed user their week.

    Bails out early when SMTP is not configured. The mailer's fallback — write
    the message to the log — is right for a password reset link somebody is
    waiting on, and wrong for a weekly summary: it would put a page of digest
    into the logs every Monday forever, for nobody.
    """
    if not mailer.configured():
        logger.info("weekly digest skipped: SMTP is not configured")
        return {"considered": 0, "sent": 0, "skipped": "smtp-not-configured"}

    db = SessionLocal()
    sent = 0
    considered = 0
    try:
        users = list(
            db.scalars(
                select(User).where(
                    User.is_active.is_(True), User.weekly_digest_enabled.is_(True)
                )
            )
        )
        for user in users:
            considered += 1
            try:
                if digest.send(db, user):
                    sent += 1
            except SoftTimeLimitExceeded:
                logger.warning(
                    "digest sweep timed out after %d of %d", considered, len(users)
                )
                break
            except Exception:
                # One user's bad week must not stop everyone else's mail.
                logger.exception("weekly digest failed for user %s", user.id)
    finally:
        db.close()

    logger.info("weekly digest: %d sent of %d considered", sent, considered)
    return {"considered": considered, "sent": sent}
