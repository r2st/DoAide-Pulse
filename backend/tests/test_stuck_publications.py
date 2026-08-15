"""A publication abandoned mid-publish has to come back.

``publish_one`` claims a row by moving it to ``publishing`` and committing, so
that a second worker handed the same id skips it. The cost of that claim is that
a worker which never returns — OOM-killed, restarted by a deploy, SIGKILLed —
leaves the row claimed with nothing running behind it. ``due_publications`` only
looks at ``pending`` and ``scheduled``, and the redelivered task's own claim
finds the row already ``publishing``, so nothing ever picks it up again.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _claimed(db, content, *, age_seconds: float, attempts: int = 1) -> Publication:
    """A row claimed *age_seconds* ago and never finished."""
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
        attempts=attempts,
    )
    db.add(row)
    db.commit()
    # Written after the insert: ``updated_at`` has an ``onupdate``, so setting
    # it on the way in would be overwritten by the flush that stores it.
    row.updated_at = utcnow() - timedelta(seconds=age_seconds)
    db.commit()
    return row


def test_a_row_left_mid_publish_is_re_armed(db, content):
    publication = _claimed(
        db, content, age_seconds=settings.publish_stuck_after_seconds + 60
    )

    assert publishing_service.reclaim_stuck(db) == 1

    db.refresh(publication)
    assert publication.status == PublicationStatus.PENDING
    assert "stopped before it finished" in (publication.error or "")


def test_a_re_armed_row_is_due_again(db, content):
    """The point of re-arming: the next sweep actually picks it up."""
    publication = _claimed(
        db, content, age_seconds=settings.publish_stuck_after_seconds + 60
    )
    assert publication.id not in [p.id for p in publishing_service.due_publications(db)]

    publishing_service.reclaim_stuck(db)

    assert publication.id in [p.id for p in publishing_service.due_publications(db)]


def test_a_row_still_inside_the_window_is_left_alone(db, content):
    """Inside the window the worker may still be alive — re-arming would double-post."""
    publication = _claimed(
        db, content, age_seconds=settings.publish_stuck_after_seconds - 60
    )

    assert publishing_service.reclaim_stuck(db) == 0

    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISHING


def test_a_row_with_no_retries_left_fails_rather_than_looping(db, content):
    """A publish that reliably kills its worker is for a human, not a loop."""
    publication = _claimed(
        db,
        content,
        age_seconds=settings.publish_stuck_after_seconds + 60,
        attempts=settings.publish_max_retries,
    )

    assert publishing_service.reclaim_stuck(db) == 1

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    assert publication.error
    # And the piece follows its publications — every one of them is terminal.
    db.refresh(content)
    assert content.status == ContentStatus.FAILED


def test_settled_rows_are_never_touched(db, content):
    rows = [
        Publication(
            content_id=content.id,
            platform=platform,
            status=status,
        )
        for platform, status in (
            (Platform.MEDIUM, PublicationStatus.PUBLISHED),
            (Platform.HASHNODE, PublicationStatus.FAILED),
            (Platform.WORDPRESS, PublicationStatus.CANCELLED),
            (Platform.BLUESKY, PublicationStatus.PENDING),
        )
    ]
    db.add_all(rows)
    db.commit()
    for row in rows:
        row.updated_at = utcnow() - timedelta(days=7)
    db.commit()

    assert publishing_service.reclaim_stuck(db) == 0
    for row in rows:
        db.refresh(row)
    assert [r.status for r in rows] == [
        PublicationStatus.PUBLISHED,
        PublicationStatus.FAILED,
        PublicationStatus.CANCELLED,
        PublicationStatus.PENDING,
    ]


def test_the_sweep_reclaims_before_it_dispatches(db, content, monkeypatch):
    """One pass, not two: the re-armed row goes out on this sweep."""
    from app.tasks import publish_tasks

    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    dispatched: list[int] = []

    def _publish_one(publication_id):
        # Returns what the real task returns. The sweep reads ``status`` off it
        # to notice its own soft time limit — ``publish_one`` absorbs that
        # exception itself, so the outcome dict is the only trace left — and a
        # double returning ``None`` stands in for a contract that has no such
        # case.
        dispatched.append(publication_id)
        return {"publication_id": publication_id, "status": "published"}

    monkeypatch.setattr(publish_tasks, "publish_one", _publish_one)
    publication = _claimed(
        db, content, age_seconds=settings.publish_stuck_after_seconds + 60
    )

    result = publish_tasks.publish_due()

    assert result == {"dispatched": 1, "failed": 0, "reclaimed": 1}
    assert dispatched == [publication.id]
