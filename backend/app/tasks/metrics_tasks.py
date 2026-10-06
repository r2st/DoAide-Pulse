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

_METRICS_BATCH_SIZE = 50


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
        # Commits no longer expire loaded objects. The sweep loads every
        # publication in one query and then iterates them, committing batches
        # of metrics along the way. Without this flag each commit expired the
        # *next* iteration's publication, forcing a re-read of the row — one
        # narrow SELECT per publication on the task whose row count grows with
        # everything the install has ever published. Same reasoning as
        # ``release_approved_content`` in ``publish_tasks``.
        db.expire_on_commit = False
        # The owner comes back as a column off the join, and is handed to
        # ``collect_metrics`` rather than walked to — see earlier comments on
        # why eager-loading the relationship was only half a fix.
        #
        # The owner's ``is_active`` is part of the WHERE for the same reason it
        # is in every other sweep here (publishing, headlines, autopilot,
        # triggers, the digest): deactivation is how an account is switched off,
        # and this was the sweep that did not notice.
        #
        # The *project*'s flag is deliberately not here, though the sweeps that
        # write do check it. Pausing a project says "write nothing new for
        # this"; it does not say "stop counting what already went out".
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
        _pending = 0
        touched_content: set[int] = set()
        rate_limited: set[publishing_service.RateLimitKey] = set()
        for publication, user_id in rows:
            publication_id = publication.id
            content_id = publication.content_id
            try:
                if (
                    publishing_service.collect_metrics(
                        db, publication, user_id=user_id, rate_limited=rate_limited,
                        commit=False,
                    )
                    is not None
                ):
                    recorded += 1
                    touched_content.add(content_id)
                    _pending += 1
                    if _pending >= _METRICS_BATCH_SIZE:
                        db.commit()
                        _pending = 0
            except SoftTimeLimitExceeded:
                logger.warning(
                    "metrics collection timed out after %d of %d publications",
                    recorded, len(rows),
                )
                break
            except Exception:
                # Isolate failures: a broken response from one platform must not
                # prevent polling the rest. Commit any pending batch first — the
                # exception comes from the platform API, before ``db.add()``, so
                # the pending rows are safe to keep.
                if _pending:
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                    _pending = 0
                else:
                    db.rollback()
                logger.exception(
                    "metrics collection failed for publication %s", publication_id
                )

        if _pending:
            db.commit()
            _pending = 0

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
