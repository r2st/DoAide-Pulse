"""How a piece's status is derived from the platforms it was sent to.

``_sync_content_status`` is the one place that decides whether a piece counts as
published, and it is asked after every attempt. The rule is asymmetric on
purpose:

* **One success is enough.** A tutorial that went out on dev.to and failed on
  Medium is published — it is on the internet, and calling it ``failed`` would
  hide it from every list the author reads.
* **Failure needs unanimity, and needs to be final.** As long as one platform is
  still going to be retried, the piece is not failed yet. This is the arm that
  matters most: getting it wrong marks a piece dead while a worker is still
  holding it, and the author sees a failure for something that then publishes.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware
from app.models.publication import Platform, Publication, PublicationStatus
from app.services.publishing_service import _sync_content_status


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="A piece",
        slug="a-piece",
        body_markdown="Body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publish(db, piece, platform, status):
    db.add(Publication(content_id=piece.id, platform=platform, status=status))
    db.commit()
    # Only the relationship is expired: a full refresh would also reload
    # ``published_at`` out of SQLite, which drops the timezone and makes the
    # "not moved by a later platform" comparison a test of the driver.
    db.expire(piece, ["publications"])


def test_one_success_among_failures_still_counts_as_published(db, piece):
    _publish(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _publish(db, piece, Platform.MEDIUM, PublicationStatus.FAILED)

    _sync_content_status(piece)

    assert piece.status == ContentStatus.PUBLISHED
    assert piece.published_at is not None


def test_published_at_is_not_moved_by_a_later_platform(db, piece):
    _publish(db, piece, Platform.DEVTO, PublicationStatus.PUBLISHED)
    _sync_content_status(piece)
    first = piece.published_at

    _publish(db, piece, Platform.MEDIUM, PublicationStatus.PUBLISHED)
    _sync_content_status(piece)

    # Through ``as_aware`` because the commit in between round-trips the column
    # via SQLite, which hands it back without a timezone.
    assert as_aware(piece.published_at) == as_aware(first)


def test_every_platform_terminal_and_none_succeeded_is_failed(db, piece):
    _publish(db, piece, Platform.DEVTO, PublicationStatus.FAILED)
    _publish(db, piece, Platform.MEDIUM, PublicationStatus.CANCELLED)

    _sync_content_status(piece)

    assert piece.status == ContentStatus.FAILED


@pytest.mark.parametrize(
    "unfinished",
    [PublicationStatus.PENDING, PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHING],
)
def test_one_platform_still_in_flight_holds_the_verdict_open(db, piece, unfinished):
    """``all(...)`` must short-circuit here — this is the arm that, wrong, shows
    the author a failure for a piece that is about to go out."""
    _publish(db, piece, Platform.DEVTO, PublicationStatus.FAILED)
    _publish(db, piece, Platform.MEDIUM, unfinished)

    _sync_content_status(piece)

    assert piece.status == ContentStatus.APPROVED
    assert piece.published_at is None


def test_a_piece_with_no_publications_is_left_alone(db, piece):
    _sync_content_status(piece)

    assert piece.status == ContentStatus.APPROVED
