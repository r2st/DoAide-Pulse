"""Re-judging headlines, and swapping in the winner where that is allowed.

Opt-in per project (``Project.auto_headline_winner``). The sweep runs daily
because engagement moves slower than that, and a title that changes under the
author more often than they look at it is a nuisance rather than an
optimisation.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.content import Content, ContentStatus
from app.models.project import Project
from app.models.user import User
from app.services import headlines
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _candidates(db: Session) -> list[Content]:
    """Published pieces whose project asked for this and that have a contest.

    An empty ``headline_history`` means the headline was never changed, so
    there is nothing to compare the live one against — no candidate, no query
    cost, no log line.

    The owner's own flag is checked as well as the project's, the same way
    ``autopilot_tasks.scan_all_projects``, ``triggers.due_triggers`` and
    ``publish_tasks.release_approved_content`` check it. Deactivating an account
    stops it signing in and stopped nothing it had already set running: this
    sweep kept rewriting the titles of its published posts on a schedule nobody
    could log in to turn off.
    """
    rows = db.scalars(
        select(Content)
        .join(Project, Project.id == Content.project_id)
        .join(User, User.id == Project.user_id)
        .where(
            Project.auto_headline_winner.is_(True),
            Project.is_active.is_(True),
            User.is_active.is_(True),
            Content.status == ContentStatus.PUBLISHED,
        )
    )
    return [row for row in rows if row.headline_history]


@celery_app.task(
    name="app.tasks.headline_tasks.auto_select_headlines",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def auto_select_headlines() -> dict:
    """Beat task: apply the winning headline wherever the evidence allows.

    Never raises for one piece's sake — a swap that fails on one post must not
    stop the rest, and none of this is urgent enough to retry aggressively.
    """
    db = SessionLocal()
    swapped = 0
    considered = 0
    try:
        candidates = _candidates(db)
        for content in candidates:
            considered += 1
            try:
                verdict, applied = headlines.auto_select(content, db)
                if applied:
                    db.commit()
                    swapped += 1
                else:
                    logger.debug(
                        "content %s headline unchanged: %s", content.id, verdict.reason
                    )
            except SoftTimeLimitExceeded:
                logger.warning(
                    "headline sweep timed out after %d of %d",
                    considered,
                    len(candidates),
                )
                db.rollback()
                break
            except Exception:
                logger.exception("headline auto-select failed for content %s", content.id)
                db.rollback()
    finally:
        db.close()

    if swapped:
        logger.info("headline sweep swapped %d of %d", swapped, considered)
    return {"considered": considered, "swapped": swapped}
