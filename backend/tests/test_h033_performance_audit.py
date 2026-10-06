"""H033 Performance Audit — tests for the three fixes.

1. ``due_publications`` is bounded by a LIMIT and ordered deterministically.
2. ``collect_metrics`` defers its commit when the caller asks.
3. The metrics sweep batches commits instead of committing per row.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishers, publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import MetricsSnapshot
from app.tasks import metrics_tasks


# ------------------------------------------------------------------ helpers --

def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)
        def close(self):
            pass
    return lambda: NoCloseProxy()


def _published(db, project, index=0, *, platform=Platform.DEVTO, scheduled_for=None):
    content = Content(
        project_id=project.id,
        title=f"Piece {index}",
        slug=f"piece-{index}",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    pub = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow(),
        external_id=f"ext-{index}",
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)
    return pub


# ---------------------------------------- due_publications is bounded --------


def test_due_publications_has_a_limit(db, project):
    """The query must carry a LIMIT so a large backlog does not load the whole
    table into memory."""
    from datetime import timedelta

    now = utcnow()
    for i in range(5):
        content = Content(
            project_id=project.id,
            title=f"Due {i}",
            slug=f"due-{i}",
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.APPROVED,
            body_markdown="body",
        )
        db.add(content)
        db.commit()
        db.add(
            Publication(
                content_id=content.id,
                platform=Platform.DEVTO,
                status=PublicationStatus.PENDING,
            )
        )
    db.commit()

    due = publishing_service.due_publications(db, now=now)
    assert len(due) == 5
    assert len(due) <= publishing_service._DUE_PUBLICATIONS_LIMIT


def test_due_publications_ordered_pending_first(db, project):
    """Pending (no scheduled_for) sorts ahead of scheduled rows."""
    from datetime import timedelta

    now = utcnow()
    ids = []
    for i, sched in enumerate([now - timedelta(hours=1), None, now - timedelta(hours=2)]):
        content = Content(
            project_id=project.id,
            title=f"Order {i}",
            slug=f"order-{i}",
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.APPROVED,
            body_markdown="body",
        )
        db.add(content)
        db.commit()
        pub = Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PENDING if sched is None else PublicationStatus.SCHEDULED,
            scheduled_for=sched,
        )
        db.add(pub)
        db.commit()
        db.refresh(pub)
        ids.append(pub.id)

    due = publishing_service.due_publications(db, now=now)
    due_ids = [p.id for p in due]

    # Pending (None scheduled_for) must come first.
    assert due_ids[0] == ids[1], "pending publication should be first"
    # Scheduled rows ordered by time: -2h before -1h.
    assert due_ids[1] == ids[2], "earlier scheduled should come second"
    assert due_ids[2] == ids[0], "later scheduled should come third"


# ---------------------------------------- collect_metrics commit flag --------


def test_collect_metrics_skips_commit_when_asked(db, user, project, monkeypatch):
    """With ``commit=False`` the row is added to the session but not committed."""
    pub = _published(db, project)
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda external_id, credentials: MetricsSnapshot(views=42),
    )

    metric = publishing_service.collect_metrics(db, pub, commit=False)

    assert metric is not None
    assert metric.views == 42
    # The row is pending in the session, not yet committed.
    assert metric in db.new
    db.commit()
    assert db.scalar(select(ContentMetric).where(ContentMetric.publication_id == pub.id)) is not None


def test_collect_metrics_commits_by_default(db, user, project, monkeypatch):
    """The default (commit=True) preserves backward compatibility."""
    pub = _published(db, project)
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda external_id, credentials: MetricsSnapshot(views=7),
    )

    metric = publishing_service.collect_metrics(db, pub)

    assert metric is not None
    assert metric not in db.new


# -------------------------------- sweep batches commits ----------------------


def test_sweep_batches_commits(db, user, project, sql_log, monkeypatch):
    """The sweep must commit in batches rather than per row, and
    ``expire_on_commit=False`` must prevent re-reads between iterations."""
    for i in range(4):
        _published(db, project, i)
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda external_id, credentials: MetricsSnapshot(views=10),
    )
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    sql_log.clear()
    result = metrics_tasks.collect_all_metrics()

    assert result["recorded"] == 4
    # With batched commits (batch size 50), 4 rows should produce at most 1
    # commit, not 4.  Count COMMIT statements (SQLite uses COMMIT, Postgres
    # uses COMMIT too).
    commits = [s for s in sql_log if s.strip().upper() == "COMMIT"]
    assert len(commits) <= 2, (
        f"expected at most 2 commits for 4 rows (batch + threshold tail), "
        f"got {len(commits)}"
    )


def test_sweep_failure_does_not_lose_prior_batch(db, user, project, monkeypatch):
    """When one publication's poll fails, the already-recorded metrics from
    earlier in the batch must still be committed."""
    for i in range(3):
        _published(db, project, i)
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()
    call_count = 0

    def _fetch(external_id, credentials):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("platform returned garbage")
        return MetricsSnapshot(views=5)

    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO), "fetch_metrics", _fetch
    )
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    result = metrics_tasks.collect_all_metrics()

    assert result["recorded"] == 2
    saved = db.scalars(select(ContentMetric)).all()
    assert len(saved) == 2, "the two successful metrics must survive the middle failure"
