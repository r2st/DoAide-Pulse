"""``GET /content/queue/publications`` — everything in flight or waiting.

The endpoint the "what is going out, and when?" panel is built on. It had no
test at all, which meant nothing pinned the two things it exists to get right:
that published rows are gone from it, and that unscheduled work sorts ahead of
work with a date on it. A queue that buries the pieces nobody has given a time
to is a queue people stop looking at.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password

QUEUE = "/api/v1/content/queue/publications"


def _content(db, project, *, title="A", slug="a") -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.APPROVED,
        title=title,
        slug=slug,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(db, content, *, status, when=None, platform=Platform.DEVTO) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=platform,
        status=status,
        scheduled_for=when,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_the_queue_holds_everything_except_what_already_went_out(
    client, auth, project, db
):
    content = _content(db, project)
    pending = _publication(db, content, status=PublicationStatus.PENDING)
    failed = _publication(
        db, content, status=PublicationStatus.FAILED, platform=Platform.MEDIUM
    )
    _publication(
        db, content, status=PublicationStatus.PUBLISHED, platform=Platform.HASHNODE
    )

    resp = client.get(QUEUE, headers=auth)

    assert resp.status_code == 200, resp.text
    assert {p["id"] for p in resp.json()} == {pending.id, failed.id}
    assert resp.headers["X-Total-Count"] == "2"


def test_work_with_no_date_on_it_sorts_ahead_of_work_that_is_scheduled(
    client, auth, project, db
):
    """Otherwise the pieces waiting on a human decision sink out of sight."""
    content = _content(db, project)
    soon = datetime.now(UTC) + timedelta(days=1)
    later = datetime.now(UTC) + timedelta(days=3)
    scheduled_later = _publication(
        db,
        content,
        status=PublicationStatus.SCHEDULED,
        when=later,
        platform=Platform.MEDIUM,
    )
    scheduled_soon = _publication(
        db,
        content,
        status=PublicationStatus.SCHEDULED,
        when=soon,
        platform=Platform.HASHNODE,
    )
    undated = _publication(db, content, status=PublicationStatus.PENDING)

    resp = client.get(QUEUE, headers=auth)

    assert [p["id"] for p in resp.json()] == [
        undated.id,
        scheduled_soon.id,
        scheduled_later.id,
    ]


def test_the_count_header_reports_the_whole_queue_not_the_page(
    client, auth, project, db
):
    content = _content(db, project)
    for platform in (Platform.DEVTO, Platform.MEDIUM, Platform.HASHNODE):
        _publication(db, content, status=PublicationStatus.PENDING, platform=platform)

    resp = client.get(QUEUE, params={"limit": 1, "offset": 1}, headers=auth)

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.headers["X-Total-Count"] == "3"


def test_another_account_s_queue_is_not_in_yours(client, auth, project, db):
    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)
    _publication(db, _content(db, theirs, title="T", slug="t"), status=PublicationStatus.PENDING)

    mine = _publication(db, _content(db, project), status=PublicationStatus.PENDING)

    resp = client.get(QUEUE, headers=auth)

    assert [p["id"] for p in resp.json()] == [mine.id]
    assert resp.headers["X-Total-Count"] == "1"
