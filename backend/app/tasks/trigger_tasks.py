"""Beat tasks for the polled trigger kinds.

Inbound webhooks need none of this — they arrive on a request thread and are
acted on there. RSS, GitHub and schedule triggers have to be gone and looked at,
so one sweep finds the ones whose interval has elapsed and dispatches each as
its own task.

Per-trigger dispatch rather than one long loop, for the same reason the repo
scan works that way: a feed that takes fifteen seconds to time out should not
delay every trigger behind it, and one crashing trigger should not take the
sweep down with it.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.trigger import Trigger
from app.services import triggers as trigger_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.trigger_tasks.check_trigger",
    soft_time_limit=180,
    time_limit=210,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def check_trigger(trigger_id: int) -> dict:
    """Poll one trigger and act on what it finds. Never raises."""
    db = SessionLocal()
    try:
        trigger = db.get(Trigger, trigger_id)
        if trigger is None or not trigger.is_active:
            return {"trigger_id": trigger_id, "status": "skipped"}
        return trigger_service.check(db, trigger)
    except SoftTimeLimitExceeded:
        logger.warning("trigger %s timed out", trigger_id)
        return {"trigger_id": trigger_id, "status": "timeout"}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("trigger %s crashed: %s", trigger_id, exc)
        return {"trigger_id": trigger_id, "status": "error", "error": str(exc)}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.trigger_tasks.check_due_triggers",
    soft_time_limit=120,
    time_limit=150,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def check_due_triggers() -> dict:
    """Beat task: dispatch every polled trigger whose interval has elapsed."""
    db = SessionLocal()
    try:
        ids = [t.id for t in trigger_service.due_triggers(db)]
    finally:
        db.close()

    dispatched = 0
    for trigger_id in ids:
        try:
            check_trigger.delay(trigger_id)
            dispatched += 1
        except Exception:
            # Broker down — fall back to inline, same as the repo scan.
            check_trigger(trigger_id)
            dispatched += 1

    if ids:
        logger.info("dispatched %d of %d due trigger(s)", dispatched, len(ids))
    return {"due": len(ids), "dispatched": dispatched}


__all__ = ["check_due_triggers", "check_trigger"]
