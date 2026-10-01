"""A row that kills its worker has to run out of retries.

``reclaim_stuck`` exists to rescue a publication whose worker died between the
claim and the outcome, and its docstring is explicit that the rescue is not
free: "The attempt is counted: it is genuinely spent, and a publish that
reliably kills its worker should exhaust its retries and be looked at by a human
rather than cycling forever."

The counting is the part that has to be checked here, because the increment it
relies on does not survive the thing it is counting. ``execute`` does
``attempts += 1`` and *flushes* — it does not commit, and nothing between that
flush and the adapter call commits either. A worker that is SIGKILLed inside
``adapter.publish`` loses its whole open transaction, so the row that
``reclaim_stuck`` finds is carrying the attempt count from *before* the attempt
that killed the worker.

Which makes the re-arm a free retry, and a payload that segfaults its worker
every time an infinite loop: claim, die, roll back, re-arm at the same count,
claim again — with no error a human would ever be shown, because the row looks
busy the whole way round.
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


def _age(db, publication: Publication, *, seconds: float) -> None:
    """Backdate the claim past the stuck cutoff.

    Written after the row is stored, because ``updated_at`` has an ``onupdate``
    that would overwrite a value set on the way in.
    """
    publication.updated_at = utcnow() - timedelta(seconds=seconds)
    db.commit()


def _killed_mid_publish(db, publication: Publication) -> None:
    """What the database holds after a worker dies inside ``adapter.publish``.

    ``publish_one`` claims the row and commits; ``execute`` then increments
    ``attempts`` and flushes inside a transaction that never commits. A SIGKILL
    rolls that back, leaving the row ``publishing`` at its *previous* attempt
    count — so this deliberately does not touch ``attempts``.
    """
    publication.status = PublicationStatus.PUBLISHING
    db.commit()
    _age(db, publication, seconds=settings.publish_stuck_after_seconds + 60)


def test_reclaiming_a_dead_worker_spends_an_attempt(db, content):
    """The rescue costs an attempt, or it is not a retry budget."""
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
        attempts=0,
    )
    db.add(publication)
    db.commit()
    _killed_mid_publish(db, publication)

    assert publishing_service.reclaim_stuck(db) == 1

    db.refresh(publication)
    assert publication.status == PublicationStatus.PENDING
    assert publication.attempts == 1, (
        "the attempt that killed the worker was rolled back with its "
        "transaction, so reclaim_stuck has to count it"
    )


def test_a_payload_that_always_kills_its_worker_stops_being_retried(db, content):
    """The loop the docstring promises is not there.

    Every pass is a worker that claims the row and dies without committing, so
    nothing but ``reclaim_stuck`` can ever move the attempt count. Given a
    budget of ``publish_max_retries``, the row has to reach a human.
    """
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
        attempts=0,
    )
    db.add(publication)
    db.commit()

    # One more pass than the budget: if the budget is real, the row is terminal
    # before this loop is done.
    for _ in range(settings.publish_max_retries + 1):
        db.refresh(publication)
        if publication.status == PublicationStatus.FAILED:
            break
        _killed_mid_publish(db, publication)
        publishing_service.reclaim_stuck(db)

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED, (
        "a publish that reliably kills its worker cycled instead of failing"
    )
    assert publication.error
    db.refresh(content)
    assert content.status == ContentStatus.FAILED
