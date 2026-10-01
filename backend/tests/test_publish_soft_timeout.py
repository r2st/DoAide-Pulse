"""A worker that runs out of time is a slow platform, not a bad post.

``publish_one`` carries a soft time limit, and Celery delivers it by raising
:class:`~celery.exceptions.SoftTimeLimitExceeded` inside whatever the task is
doing. In practice that is always the same place: the HTTP call in
``adapter.publish``, because that is where a publish spends its time.

Which puts it inside :func:`publishing_service.execute`'s ``try``. And
``SoftTimeLimitExceeded`` is an ordinary ``Exception`` — not a ``BaseException``
like the timeouts some libraries use — so the defensive ``except Exception`` at
the bottom of ``execute`` caught it, wrote "Unexpected error" on the row and
marked it **terminal**. First time a platform was slow, the post was burned, and
the only way back was a hand-retry by somebody with no reason to be looking.

``publish_one`` has a handler for exactly this, and its comment says the row
should come back — "Record the timeout so it shows up in the UI and can be
retried". It never ran. ``execute`` swallowed the exception and returned
normally, so there was nothing left to propagate.

A timeout is the most transient failure there is. It belongs on the retry
budget with every other one.
"""
from __future__ import annotations

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service
from app.services.crypto import encrypt_credentials


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Pulse 1.0 is out.",
        meta_description="Pulse 1.0 is out.",
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@r2st",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def times_out(monkeypatch):
    """An adapter that runs past the worker's soft limit."""
    from app.services.publishers.devto import DevToAdapter

    def publish(self, request, credentials):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(DevToAdapter, "publish", publish)


def _queued(db, content) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(row)
    db.commit()
    return row


def test_a_timeout_is_retryable_not_terminal(db, content, connected, times_out):
    publication = _queued(db, content)

    publishing_service.execute(db, publication)

    db.refresh(publication)
    assert publication.status != PublicationStatus.FAILED, (
        "one slow platform response burned the post permanently"
    )
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for is not None


def test_the_piece_does_not_follow_a_timeout_to_failed(
    db, content, connected, times_out
):
    """The content row is what the user sees on the dashboard."""
    publication = _queued(db, content)

    publishing_service.execute(db, publication)

    db.refresh(content)
    assert content.status != ContentStatus.FAILED


def test_a_timeout_says_so_on_the_row(db, content, connected, times_out):
    """"Unexpected error: " is what an unhandled bug looks like, and this is not one."""
    publication = _queued(db, content)

    publishing_service.execute(db, publication)

    db.refresh(publication)
    error = publication.error or ""
    assert "Unexpected error" not in error
    assert "timed out" in error.lower()


def test_a_timeout_still_spends_its_attempt(db, content, connected, times_out):
    publication = _queued(db, content)

    publishing_service.execute(db, publication)

    db.refresh(publication)
    assert publication.attempts == 1


def test_a_platform_that_always_times_out_eventually_stops(
    db, content, connected, times_out
):
    """Retryable is not the same as infinite — the budget still ends it."""
    publication = _queued(db, content)

    for _ in range(settings.publish_max_retries):
        publication.status = PublicationStatus.PENDING
        db.commit()
        publishing_service.execute(db, publication)

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    db.refresh(content)
    assert content.status == ContentStatus.FAILED
