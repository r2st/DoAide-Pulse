"""Re-judging headlines, swapping in the winner, and telling the destinations.

Opt-in per project (``Project.auto_headline_winner``). The sweep runs daily
because engagement moves slower than that, and a title that changes under the
author more often than they look at it is a nuisance rather than an
optimisation.

Every swap is followed by a push to the destinations that can carry it. That
ordering is deliberate and it is the only one that is safe: the swap is a local
write that always succeeds, the push is a network call to up to four platforms
that may not. Doing the push first would mean retitling live posts and then
failing to record which headline they now show.
"""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, lazyload

from app.database import SessionLocal
from app.models.content import Content, ContentStatus
from app.models.project import Project
from app.models.user import User
from app.services import headline_sync, headlines
from app.tasks.celery_app import task

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
        # This sweep is unbounded by design — every published piece on every
        # project that asked for it — so the ``lazy="selectin"`` default was
        # fetching every publication on the install once per beat. Nothing here
        # reads them: ``headlines.auto_select`` reaches metrics through its own
        # join on ``Publication`` and otherwise only writes a title.
        .options(lazyload(Content.publications))
        .where(
            Project.auto_headline_winner.is_(True),
            Project.is_active.is_(True),
            User.is_active.is_(True),
            Content.status == ContentStatus.PUBLISHED,
        )
    )
    return [row for row in rows if row.headline_history]


@task(
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
    # One applied swap commits, and a commit expires every candidate still to
    # come — so the sweep re-read each remaining piece's whole ``Content``, body
    # included, to write a title. ``_candidates`` is unbounded by design, so the
    # cost of the first swap was one wide read per published piece on every
    # project that opted in. Every other test of this sweep has a single
    # candidate, which is the size at which that cannot show up.
    #
    # Safe here: each candidate is visited once and nothing re-reads a row a
    # commit changed. The error arms below still ``rollback()``, which expires
    # everything regardless — after a failure the loaded state is exactly what
    # should not be trusted.
    db.expire_on_commit = False
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
                    # Inline rather than dispatched: this is already a worker,
                    # and a separate task would race the next beat's read of
                    # ``live_title``. Never raises — see ``sync_title``.
                    _log_sync(content.id, headline_sync.sync_title(db, content))
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
                # Rollback first: the ``db.commit()`` above is inside the ``try``, and
                # a commit that fails leaves the session unable to emit SQL — so
                # reading ``content.id`` to name the row in the log needed a SELECT
                # the session would refuse, and the ``PendingRollbackError`` that
                # raised escaped this handler and ended the sweep. Rolling back first
                # both unpoisons the session for the next candidate and makes the log
                # line reachable.
                db.rollback()
                logger.exception("headline auto-select failed for content %s", content.id)
    finally:
        db.close()

    if swapped:
        logger.info("headline sweep swapped %d of %d", swapped, considered)
    return {"considered": considered, "swapped": swapped}


def _log_sync(content_id: int, outcomes: list[headline_sync.SyncOutcome]) -> None:
    """One line per swap saying how far the new headline actually got.

    Worth a log line even when everything worked: "retitled 1 of 4 destinations,
    3 cannot carry a title change" is the sentence that explains a headline
    contest which never reaches a verdict, and the alternative is working it out
    from the platform list by hand.
    """
    if not outcomes:
        return
    reached = sum(1 for outcome in outcomes if outcome.reached)
    failed = [o for o in outcomes if o.status == headline_sync.FAILED]
    logger.info(
        "content %s headline reached %d of %d destinations%s",
        content_id,
        reached,
        len(outcomes),
        "" if not failed else f"; failed on {', '.join(o.platform.value for o in failed)}",
    )


@task(
    name="app.tasks.headline_tasks.sync_headline",
    soft_time_limit=120,
    time_limit=150,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=2,
)
def sync_headline(content_id: int) -> dict:
    """Push one piece's current headline to every destination that can take it.

    Dispatched by ``POST /content/{id}/headlines/apply``, which has already
    changed the title by the time this runs: a human swapped the headline and
    the live posts have to be told. Kept off the request thread because it is up
    to four platform calls, and the swap itself is complete without them.

    Idempotent, and cheap when there is nothing to do — a destination already
    showing the title is an ``unchanged`` outcome and no request.
    """
    db = SessionLocal()
    try:
        content = db.get(Content, content_id)
        if content is None:
            logger.info("headline sync skipped: content %s is gone", content_id)
            return {"content_id": content_id, "outcomes": []}
        outcomes = headline_sync.sync_title(db, content)
        _log_sync(content_id, outcomes)
        return {
            "content_id": content_id,
            "outcomes": [outcome.as_dict() for outcome in outcomes],
        }
    finally:
        db.close()
