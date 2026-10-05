"""Poll published posts for engagement numbers."""
from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models.content import Content
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.services import engagement_alerts, publishers, publishing_service
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


@task(
    name="app.tasks.metrics_tasks.collect_all_metrics",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def collect_all_metrics() -> dict:
    """Beat task: snapshot engagement for everything published.

    Only platforms whose adapter reports metrics are polled — see
    ``Adapter.supports_metrics``. Skipping the rest keeps this from making a
    round trip per post to a platform that has no stats API at all.
    """
    metric_platforms = [
        adapter.platform
        for adapter in publishers.all_adapters()
        if adapter.implemented and adapter.supports_metrics
    ]
    if not metric_platforms:
        return {"polled": 0, "recorded": 0, "crossings": 0}

    db = SessionLocal()
    try:
        # The owner comes back as a column off the join, and is handed to
        # ``collect_metrics`` rather than walked to.
        #
        # Eager-loading the two hops instead — which is what this did — was only
        # half a fix, and the half that showed up in a test where
        # ``collect_metrics`` is stubbed and never commits. In production it
        # records a metric per publication, and each commit expires the session:
        # the next iteration re-read the publication, then re-read its whole
        # ``Content`` (article body and all) and its project, one publication at
        # a time, exactly as if nothing had been eagerly loaded. Three SELECTs
        # per row on the sweep whose row count grows with everything the install
        # has ever published, one of them carrying an article.
        #
        # Not traversing the relationship at all is what survives the commits.
        # The publication itself is still re-read after each one — it is the row
        # being written about, and ``collect_metrics`` re-checks its status
        # before polling — but that is one narrow SELECT, not three wide ones.
        #
        # The owner's ``is_active`` is part of the WHERE for the same reason it
        # is in every other sweep here (publishing, headlines, autopilot,
        # triggers, the digest): deactivation is how an account is switched off,
        # and this was the sweep that did not notice. What it polls with is the
        # account's own stored platform credentials — ``collect_metrics`` takes
        # *user_id* precisely to look them up — so a deactivated account went on
        # making authenticated requests to Dev.to and Hashnode under its owner's
        # tokens, every few hours, indefinitely. That is the shape
        # :func:`app.services.preview_links.resolve` argues about: switching an
        # account off has to close the doors it opened, and a scheduled job
        # holding its credentials is one of them.
        #
        # The *project*'s flag is deliberately not here, though the sweeps that
        # write do check it. Pausing a project says "write nothing new for
        # this"; it does not say "stop counting what already went out", and the
        # views still accruing on its posts are its owner's numbers to come back
        # to.
        rows = db.execute(
            select(Publication, Project.user_id)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .join(User, User.id == Project.user_id)
            .where(
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.platform.in_(metric_platforms),
                Publication.external_id.is_not(None),
                User.is_active.is_(True),
            )
        ).all()
        recorded = 0
        # The pieces this sweep wrote a fresh snapshot for. Collected so the
        # threshold check at the end looks only at what actually moved: a piece
        # nobody polled cannot have crossed anything, and evaluating the whole
        # install every few hours would be a full scan for an answer that is
        # ``no`` for all but a handful of rows in its lifetime.
        touched_content: set[int] = set()
        # Shared across the whole sweep: once a platform rate-limits one
        # account, the rest of that account's posts there are skipped without a
        # request. See ``publishing_service.collect_metrics``.
        rate_limited: set[publishing_service.RateLimitKey] = set()
        for publication, user_id in rows:
            # Read before the call, not inside the handler below. ``collect_metrics``
            # ends in a commit, and a commit that fails leaves the session unable to
            # emit SQL until it is rolled back — including the SELECT that reading an
            # expired ``publication.id`` would need. Asking for the id *while* handling
            # the failure raised ``PendingRollbackError`` from inside the ``except``
            # arm, which escaped the loop, the ``try``, and the task: one row whose
            # write would not land ended the whole sweep, and the rows after it were
            # never polled.
            publication_id = publication.id
            # Read alongside the id, and for the same reason: after
            # ``collect_metrics`` commits, the publication is expired and
            # touching any attribute is another SELECT — inside an ``except``
            # arm, potentially against a session that cannot emit SQL.
            content_id = publication.content_id
            try:
                if (
                    publishing_service.collect_metrics(
                        db, publication, user_id=user_id, rate_limited=rate_limited
                    )
                    is not None
                ):
                    recorded += 1
                    touched_content.add(content_id)
            except SoftTimeLimitExceeded:
                logger.warning(
                    "metrics collection timed out after %d of %d publications",
                    recorded, len(rows),
                )
                break
            except Exception:
                # Isolate failures: a broken response from one platform must not
                # prevent polling the rest.
                #
                # The rollback is what makes that true when the failure came from
                # the database rather than the platform. Every other sweep in this
                # tree rolls back here; this one did not, so a failed write left the
                # session poisoned and every remaining publication failed too — on an
                # error that says nothing about them.
                db.rollback()
                logger.exception(
                    "metrics collection failed for publication %s", publication_id
                )

        # After the loop, not inside it. A piece publishes to several platforms
        # and each one is a separate row here; checking per row would evaluate
        # the same piece four times in one sweep and — before the latch was
        # written — announce it on whichever platform happened to be polled
        # first, with only that platform's share of the number.
        #
        # Outside the per-row try/except, and with its own: this is the tail of
        # the sweep, and a notification that cannot be assembled must not turn
        # a successful poll of every platform into a failed task.
        try:
            crossings = engagement_alerts.evaluate(db, sorted(touched_content))
        except Exception:
            db.rollback()
            crossings = []
            logger.exception("engagement threshold check failed")
    finally:
        db.close()

    logger.info(
        "metrics: polled %d, recorded %d, thresholds crossed %d",
        len(rows), recorded, len(crossings),
    )
    return {"polled": len(rows), "recorded": recorded, "crossings": len(crossings)}


@task(
    name="app.tasks.metrics_tasks.collect_one",
    soft_time_limit=60,
    time_limit=90,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
)
def collect_one(publication_id: int) -> dict:
    """Poll a single publication — used by the "refresh" button."""
    db = SessionLocal()
    try:
        publication = db.get(Publication, publication_id)
        if publication is None:
            return {"publication_id": publication_id, "recorded": False}
        metric = publishing_service.collect_metrics(db, publication)
        return {"publication_id": publication_id, "recorded": metric is not None}
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("collect_one timed out for publication %s", publication_id)
        return {"publication_id": publication_id, "recorded": False}
    except Exception:
        db.rollback()
        logger.exception(
            "collect_one failed for publication %s", publication_id
        )
        return {"publication_id": publication_id, "recorded": False}
    finally:
        db.close()
