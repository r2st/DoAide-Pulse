"""Scheduled content that has not been routed to a platform yet.

A piece can carry a ``scheduled_for`` before anyone has said where it goes —
that is exactly what dragging a draft onto a date does. If the calendar only
read publications, the drop would appear to work and the entry would vanish on
the next load, which is the worst shape a scheduling bug can take.

The window filter for these rows is applied in Python rather than SQL (there is
no publication to join through), so it needs its own tests: a row a day outside
the window must not appear, and a row inside it must.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus


def _now():
    return datetime.now(UTC)


def _draft(db, project, *, title, slug, when, status=ContentStatus.APPROVED):
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=title,
        slug=slug,
        status=status,
        scheduled_for=when,
    )
    db.add(row)
    db.flush()
    return row


def test_content_scheduled_but_routed_nowhere_still_appears(client, auth, db, project):
    """The drag-and-drop case. No publication row exists yet, so the entry has
    no ``publication_id`` and no platform — and it is movable, because nothing
    has claimed it."""
    _draft(db, project, title="Unrouted", slug="unrouted", when=_now() + timedelta(days=2))
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["title"] == "Unrouted"
    assert entries[0]["publication_id"] is None
    assert entries[0]["platform"] is None
    assert entries[0]["movable"] is True


def test_unrouted_content_outside_the_window_is_left_out(client, auth, db, project):
    """The window is the whole point of asking for a month at a time. A piece
    scheduled next year must not arrive in this month's payload just because it
    has no publication to filter on in SQL."""
    _draft(db, project, title="Far future", slug="far-future", when=_now() + timedelta(days=400))
    _draft(db, project, title="Nearby", slug="nearby", when=_now() + timedelta(days=1))
    db.commit()

    # Naive ISO strings: a '+' in a query string decodes as a space.
    start = (_now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (_now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    resp = client.get(f"/api/v1/calendar?start={start}&end={end}", headers=auth)

    assert resp.status_code == 200
    titles = [e["title"] for e in resp.json()["entries"]]
    assert titles == ["Nearby"]


def test_unrouted_content_before_the_window_is_left_out(client, auth, db, project):
    _draft(db, project, title="Last year", slug="last-year", when=_now() - timedelta(days=300))
    db.commit()

    start = (_now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (_now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    resp = client.get(f"/api/v1/calendar?start={start}&end={end}", headers=auth)

    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_published_content_is_not_listed_as_unrouted(client, auth, db, project):
    """A published piece keeps its ``scheduled_for``. Without the status filter
    it would show up twice — once as its publication, once as a ghost draft."""
    content = _draft(
        db,
        project,
        title="Live already",
        slug="live-already",
        when=_now() - timedelta(days=1),
        status=ContentStatus.PUBLISHED,
    )
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=_now() - timedelta(days=1),
        )
    )
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)

    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["publication_id"] is not None


def test_moving_one_publication_leaves_the_content_schedule_alone(
    client, auth, db, project
):
    """Naming a ``publication_id`` means "move this one destination".

    Writing the content's own ``scheduled_for`` too would drag every *other*
    platform's calendar position with it — the user asked to move one pin, not
    the whole piece.
    """
    original = _now() + timedelta(days=3)
    content = _draft(db, project, title="Cross-post", slug="cross-post", when=original)
    devto = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=original,
    )
    hashnode = Publication(
        content_id=content.id,
        platform=Platform.HASHNODE,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=original,
    )
    db.add_all([devto, hashnode])
    db.commit()
    db.refresh(devto)
    db.refresh(hashnode)

    moved = _now() + timedelta(days=9)
    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": moved.isoformat(), "publication_id": devto.id},
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    db.refresh(content)
    db.refresh(hashnode)
    # SQLite hands these back naive; the instant is what the assertion is about.
    assert content.scheduled_for.replace(tzinfo=UTC) == original
    assert hashnode.scheduled_for.replace(tzinfo=UTC) == original


def test_moving_the_whole_piece_moves_the_content_schedule_too(
    client, auth, db, project
):
    original = _now() + timedelta(days=3)
    content = _draft(db, project, title="All of it", slug="all-of-it", when=original)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=original,
        )
    )
    db.commit()

    moved = _now() + timedelta(days=9)
    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": moved.isoformat()},
    )

    assert resp.status_code == 200, resp.text
    db.refresh(content)
    assert content.scheduled_for is not None
    assert abs(
        content.scheduled_for.replace(tzinfo=UTC) - moved
    ) < timedelta(seconds=2)


def test_rescheduling_an_unknown_publication_of_a_real_piece_is_404(
    client, auth, db, project
):
    content = _draft(db, project, title="Solo", slug="solo", when=_now() + timedelta(days=1))
    db.commit()

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={
            "scheduled_for": (_now() + timedelta(days=4)).isoformat(),
            "publication_id": 9999,
        },
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Publication not found."
