"""Webhook delivery: one task for a single row, one sweep for the backlog.

Both exist because a delivery has two lives. The first attempt happens moments
after the event, dispatched by whatever produced it. Every attempt after that is
a backoff the producer is long gone for, so something periodic has to come back
for it.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.webhook import WebhookDelivery
from app.services import webhooks
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
    name="app.tasks.webhook_tasks.deliver_one",
    soft_time_limit=60,
    time_limit=90,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
)
def deliver_one(delivery_id: int) -> dict:
    """POST one delivery.

    Only a *database* failure retries at the Celery level. An endpoint that
    refuses or times out is not an error here — it is the outcome, recorded on
    the row with a ``next_attempt_at``, and re-running the task would spend a
    retry the delivery is already tracking.
    """
    db = SessionLocal()
    try:
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            # Endpoint deleted between dispatch and pickup. Not an error.
            return {"delivery_id": delivery_id, "status": "gone"}
        webhooks.deliver(db, delivery)
        return {"delivery_id": delivery_id, "status": delivery.status.value}
    finally:
        db.close()


@task(
    name="app.tasks.webhook_tasks.deliver_due",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def deliver_due() -> dict:
    """Beat task: every pending delivery whose backoff has elapsed."""
    db = SessionLocal()
    attempted = 0
    delivered = 0
    try:
        due = webhooks.due_deliveries(db)
        for delivery in due:
            attempted += 1
            try:
                webhooks.deliver(db, delivery)
                if delivery.status.value == "delivered":
                    delivered += 1
            except SoftTimeLimitExceeded:
                logger.warning(
                    "webhook sweep timed out after %d of %d", attempted, len(due)
                )
                break
            except Exception:
                # One endpoint's bad day must not stop the rest of the queue.
                logger.exception("webhook delivery %s failed", delivery.id)
    finally:
        db.close()

    if attempted:
        logger.info("webhook sweep: %d delivered of %d attempted", delivered, attempted)
    return {"attempted": attempted, "delivered": delivered}
