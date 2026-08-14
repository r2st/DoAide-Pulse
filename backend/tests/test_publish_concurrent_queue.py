"""Two requests queueing the same piece for the same platform.

``queue`` decides "re-arm the existing row" or "insert a new one" from
``content.publications``, which is only as fresh as the moment the caller loaded
the piece. Two requests that both read it before either wrote — a double-clicked
Publish button is the whole of what that takes — both saw nothing for the
platform and both inserted, and ``uq_publication_content_platform`` refused the
second. The user got a 500 for pressing a button twice.

``test_publish_platform_dedupe.py`` covers the same constraint reached the other
way: one *request* naming a platform twice. That one is caught in the schema
before ``queue`` ever runs, because the whole list is in front of it. This one
cannot be — the conflicting row is not in the request, it is in the database,
and it arrives between the read and the write.

The race is staged rather than threaded: the test suite runs on one SQLite
connection, so two real sessions could not interleave. What production hands
``queue`` is a piece whose ``publications`` collection was loaded before the
other writer committed, and ``set_committed_value`` produces exactly that
collection without needing a second connection. The constraint underneath is
real, and it is what the assertions are about.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


@pytest.fixture
def approved(db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Shipping v2",
        slug="shipping-v2",
        body_markdown="It is out.",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


def _stale(content: Content) -> None:
    """Put *content* back to what it looked like before the other writer wrote.

    Not a rewind of the database — only of this instance's idea of it, which is
    the half of the race that matters.
    """
    set_committed_value(content, "publications", [])


def _rows(db, content) -> list[Publication]:
    return list(
        db.scalars(
            select(Publication)
            .where(Publication.content_id == content.id)
            .order_by(Publication.platform)
        )
    )


def test_the_second_request_re_arms_the_row_the_first_one_wrote(db, approved):
    """No constraint error, no second row, and the winner's row is the row."""
    winner = publishing_service.queue(db, approved, [Platform.DEVTO])[0]
    db.commit()
    winner_id = winner.id
    _stale(approved)

    out = publishing_service.queue(db, approved, [Platform.DEVTO])
    db.commit()

    assert [p.id for p in out] == [winner_id]
    assert [p.id for p in _rows(db, approved)] == [winner_id]


def test_the_second_request_still_applies_its_own_settings(db, approved):
    """Re-arming is not the same as giving up: the loser's request still lands."""
    publishing_service.queue(db, approved, [Platform.DEVTO])
    db.commit()
    _stale(approved)

    when = utcnow() + timedelta(hours=3)
    out = publishing_service.queue(
        db, approved, [Platform.DEVTO], scheduled_for=when, as_draft=True
    )
    db.commit()

    assert len(out) == 1
    row = _rows(db, approved)[0]
    assert row.status is PublicationStatus.SCHEDULED
    assert row.as_draft is True
    assert row.scheduled_for is not None


def test_a_platform_the_winner_already_published_is_not_re_armed(db, approved):
    """The "already live" branch has to survive the retry, or this double-posts.

    The retry re-reads the collection, and the rows it comes back with are the
    same instances the first attempt had already set to ``pending``. Reading
    ``status`` off one of those without putting it back in step with the database
    would answer "not published" about a row that is live.
    """
    publishing_service.queue(db, approved, [Platform.DEVTO])
    db.commit()
    live = _rows(db, approved)[0]
    live.status = PublicationStatus.PUBLISHED
    live.external_id = "already-out-there"
    live.external_url = "https://dev.to/x/already-out-there"
    live.published_at = utcnow()
    db.commit()
    _stale(approved)

    out = publishing_service.queue(db, approved, [Platform.DEVTO])
    db.commit()

    assert len(out) == 1
    row = _rows(db, approved)[0]
    assert row.status is PublicationStatus.PUBLISHED, "a live post was re-queued"
    assert row.external_id == "already-out-there"


def test_the_platforms_that_did_not_conflict_are_still_queued(db, approved):
    """One conflicting destination must not cost the batch the others."""
    publishing_service.queue(db, approved, [Platform.DEVTO])
    db.commit()
    _stale(approved)

    out = publishing_service.queue(
        db, approved, [Platform.DEVTO, Platform.MASTODON]
    )
    db.commit()

    assert {p.platform for p in out} == {Platform.DEVTO, Platform.MASTODON}
    rows = _rows(db, approved)
    assert {r.platform for r in rows} == {Platform.DEVTO, Platform.MASTODON}
    assert all(r.status is not PublicationStatus.FAILED for r in rows)


def test_the_retry_does_not_undo_work_the_caller_had_already_flushed(db, project):
    """The savepoint is what makes this safe to use from the generation pipeline.

    ``content_pipeline.generate_and_route`` writes the piece and then queues it
    in one transaction. A plain rollback on the conflict would take the article
    with it; the savepoint takes back only the arming.
    """
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Written then queued",
        slug="written-then-queued",
        body_markdown="Body that must survive the retry.",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()

    publishing_service.queue(db, content, [Platform.DEVTO])
    db.flush()
    _stale(content)

    publishing_service.queue(db, content, [Platform.DEVTO])
    db.commit()

    survived = db.scalars(
        select(Content).where(Content.slug == "written-then-queued")
    ).one()
    assert survived.body_markdown == "Body that must survive the retry."
    assert len(_rows(db, survived)) == 1
