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
from app.services.errors import sanitize_unexpected_error
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
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
        return {"trigger_id": trigger_id, "status": "error", "error": sanitize_unexpected_error(exc)}
    finally:
        db.close()


@task(
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
    reclaimed = 0
    try:
        # Before the dispatch, the same way ``publish_tasks.publish_due`` puts
        # ``reclaim_stuck`` before its own query. A firing whose worker died is
        # not something this sweep will rediscover — its dedupe key stops the
        # source from ever offering it again — so if this sweep does not settle
        # it, nothing does. See ``triggers.reclaim_stuck_events``.
        reclaimed = trigger_service.reclaim_stuck_events(db)
        ids = [t.id for t in trigger_service.due_triggers(db)]
    finally:
        db.close()

    dispatched = 0
    failed = 0
    inline = 0
    # One warning per sweep, not per trigger: the broker is a single shared
    # thing, so a hundred due triggers would otherwise log the same outage a
    # hundred times and bury it in its own noise.
    broker_warned = False
    for trigger_id in ids:
        # Set from the inline branch's *return value*, not from an exception —
        # see the break at the bottom of the loop.
        out_of_time = False
        try:
            check_trigger.delay(trigger_id)
        except SoftTimeLimitExceeded:
            # Before the broker fallback, not after: the timeout arrives as an
            # Exception, so the handler below would read it as "broker down"
            # and answer by running a full check *inline* — the most expensive
            # thing available — on a task that is already out of time. The rest
            # stay due and the next tick dispatches them.
            logger.warning(
                "trigger dispatch timed out after %d of %d", dispatched, len(ids)
            )
            break
        except Exception as exc:
            # Broker down — fall back to inline, same as the repo scan.
            #
            # And say so. This swallowed the error and then reported
            # "dispatched N of N", which is the opposite of what happened:
            # nothing was enqueued and the beat worker polled every due trigger
            # itself, serially, inside its own soft time limit. The only symptom
            # an operator saw was the sweep timing out with no stated cause —
            # while the three request-path dispatchers all warn on this branch.
            if not broker_warned:
                logger.warning("broker unavailable, checking triggers inline: %s", exc)
                broker_warned = True
            inline += 1
            try:
                out_of_time = check_trigger(trigger_id).get("status") == "timeout"
            except Exception:
                # The same rule ``publish_due`` states outright: one trigger's
                # bad day must not end the pass. ``check_trigger`` promises never
                # to raise, but the promise is made by a ``try`` it enters after
                # opening its session — so ``SessionLocal()`` and the ``close()``
                # in its ``finally`` are both outside it, and land here.
                #
                # Escaping the loop would not lose one trigger, it would drop
                # every later one that was due, and the next sweep selects the
                # same ids in the same order and dies in the same place.
                logger.exception("inline check of trigger %s failed", trigger_id)
                failed += 1
                continue
        dispatched += 1
        if out_of_time:
            # The inline branch is serial and spends *this* task's budget, so it
            # is where the soft limit actually lands — but it never arrives here
            # as an exception. ``check_trigger`` catches its own
            # ``SoftTimeLimitExceeded`` to report the timeout, and Celery raises
            # it once, so by the time control is back in this loop the only trace
            # left is the outcome it returned.
            #
            # Read as an ordinary result, the sweep carried on to the next
            # trigger — a fresh feed fetch, started after the deadline — and the
            # one after that, until the hard limit killed the worker outright.
            # ``acks_late`` then redelivered the whole batch. The remaining
            # triggers stay due and the next tick dispatches them.
            logger.warning(
                "trigger sweep timed out checking inline after %d of %d",
                dispatched,
                len(ids),
            )
            break

    if ids:
        logger.info(
            "dispatched %d of %d due trigger(s), %d inline, %d failed",
            dispatched,
            len(ids),
            inline,
            failed,
        )
    return {
        "due": len(ids),
        "dispatched": dispatched,
        "failed": failed,
        "reclaimed": reclaimed,
    }


__all__ = ["check_due_triggers", "check_trigger"]
