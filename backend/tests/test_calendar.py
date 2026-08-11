"""Content calendar endpoint tests.

The calendar combines publications and unrouted content into a unified timeline.
These tests verify the filtering, movability flags, and reschedule behaviour.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus


def _now():
    return datetime.now(UTC)


def test_empty_calendar(client, auth):
    resp = client.get("/api/v1/calendar", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert isinstance(body["cadence"], list)
    assert isinstance(body["suggested_slots"], list)


def test_calendar_includes_scheduled_publication(client, auth, db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Upcoming post",
        slug="upcoming-post",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    future = _now() + timedelta(days=3)
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=future,
    )
    db.add(pub)
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["title"] == "Upcoming post"
    assert entries[0]["movable"] is True


def test_published_entry_is_not_movable(client, auth, db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Already live",
        slug="already-live",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.HASHNODE,
        status=PublicationStatus.PUBLISHED,
        published_at=_now() - timedelta(days=1),
    )
    db.add(pub)
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["movable"] is False


def test_reschedule_moves_publication(client, auth, db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="Movable",
        slug="movable",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)

    new_time = (_now() + timedelta(days=5)).isoformat()
    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": new_time},
    )
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "scheduled"


def test_reschedule_published_is_409(client, auth, db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Done",
        slug="done",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.MEDIUM,
        status=PublicationStatus.PUBLISHED,
        published_at=_now(),
    )
    db.add(pub)
    db.commit()

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": (_now() + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 409


def _movable(db, project, *, title, status=PublicationStatus.PENDING):
    """A piece with one publication in *status*, ready to be dragged."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title=title,
        slug=title.lower().replace(" ", "-"),
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    pub = Publication(content_id=content.id, platform=Platform.DEVTO, status=status)
    db.add(pub)
    db.commit()
    db.refresh(pub)
    return content, pub


def test_reschedule_refuses_a_time_in_the_past(client, auth, db, project):
    """Dropping a card behind "now" is a publish on the next sweep, not a plan.

    The publish and schedule endpoints have always refused this; drag-and-drop
    lands here instead and was the one surface without the guard.
    """
    content, pub = _movable(db, project, title="Dragged back")

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": (_now() - timedelta(days=3)).isoformat()},
    )
    assert resp.status_code == 422
    assert "in the past" in resp.json()["detail"]

    db.refresh(pub)
    assert pub.scheduled_for is None
    assert pub.status == PublicationStatus.PENDING


def test_reschedule_refuses_a_mistyped_year(client, auth, db, project):
    """2126 for 2026 parks a post for a century rather than scheduling it."""
    content, _ = _movable(db, project, title="Dragged far")

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": (_now() + timedelta(days=365 * 100)).isoformat()},
    )
    assert resp.status_code == 422
    assert "check the year" in resp.json()["detail"]


def test_reschedule_accepts_clearing_the_time(client, auth, db, project):
    """A null is "go out on the next sweep", which normalize passes through."""
    content, pub = _movable(db, project, title="Cleared")
    pub.status = PublicationStatus.SCHEDULED
    pub.scheduled_for = _now() + timedelta(days=2)
    db.commit()

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": None},
    )
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "pending"


def test_reschedule_refuses_a_publication_already_going_out(client, auth, db, project):
    """A row a worker has claimed must not be re-armed underneath it.

    Moving a PUBLISHING row back to SCHEDULED means the next sweep picks up a
    publication that is about to succeed — and posts it a second time. The
    calendar already reports these as ``movable=False``.
    """
    content, pub = _movable(
        db, project, title="In flight", status=PublicationStatus.PUBLISHING
    )

    resp = client.patch(
        f"/api/v1/calendar/content/{content.id}",
        headers=auth,
        json={"scheduled_for": (_now() + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 409
    assert "going out now" in resp.json()["detail"]

    db.refresh(pub)
    assert pub.status == PublicationStatus.PUBLISHING


def test_reschedule_nonexistent_content_is_404(client, auth):
    resp = client.patch(
        "/api/v1/calendar/content/9999",
        headers=auth,
        json={"scheduled_for": (_now() + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 404


def test_cadence_guide_returns_data(client, auth):
    resp = client.get("/api/v1/calendar/cadence", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) > 0
    for entry in body:
        assert "platform" in entry
        assert "max_per_week" in entry


def test_cadence_guide_single_platform(client, auth):
    resp = client.get("/api/v1/calendar/cadence?platform=devto", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["platform"] == "devto"


def test_calendar_rejects_window_over_365_days(client, auth):
    """A window wider than a year is refused to prevent expensive queries."""
    # Use naive ISO strings — a '+' in the query string is decoded as a space.
    far_start = (_now() - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S")
    far_end = _now().strftime("%Y-%m-%dT%H:%M:%S")
    resp = client.get(
        f"/api/v1/calendar?start={far_start}&end={far_end}",
        headers=auth,
    )
    assert resp.status_code == 400
    assert "365" in resp.json()["detail"]


def test_calendar_accepts_window_under_365_days(client, auth):
    """A 60-day window is well within the limit."""
    start = (_now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (_now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    resp = client.get(
        f"/api/v1/calendar?start={start}&end={end}",
        headers=auth,
    )
    assert resp.status_code == 200


def test_calendar_requires_auth(client):
    assert client.get("/api/v1/calendar").status_code == 401


def test_calendar_filters_by_project(client, auth, db, project):
    """Passing project_id filters to just that project's entries."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Mine",
        slug="mine",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=_now() + timedelta(days=2),
    )
    db.add(pub)
    db.commit()

    # Correct project_id → entry appears
    resp = client.get(f"/api/v1/calendar?project_id={project.id}", headers=auth)
    assert len(resp.json()["entries"]) == 1

    # Wrong project_id → empty
    resp = client.get("/api/v1/calendar?project_id=9999", headers=auth)
    assert len(resp.json()["entries"]) == 0


def test_a_publication_with_no_time_at_all_is_not_on_the_calendar(
    client, auth, db, project
):
    """A PENDING row with neither published_at nor scheduled_for has no position.

    The window filter is ``published_at BETWEEN ... OR scheduled_for BETWEEN
    ...``, and NULL never satisfies BETWEEN, so such a row is excluded in SQL
    rather than skipped in Python. That is what lets the loop treat
    ``published_at or scheduled_for`` as a real datetime. If this ever starts
    returning an entry, the calendar is about to be asked to place something
    that has no date.
    """
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Queued, unscheduled",
        slug="queued-unscheduled",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PENDING,
            scheduled_for=None,
            published_at=None,
        )
    )
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)

    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_a_published_row_with_no_published_at_falls_back_to_its_slot(
    client, auth, db, project
):
    """``published_at or scheduled_for`` picks the second when the first is unset.

    A row can be marked published while the timestamp write is still in
    flight; it still has to land somewhere on the calendar rather than
    vanishing from it.
    """
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Live, timestamp pending",
        slug="live-timestamp-pending",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    slot = _now() - timedelta(days=1)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            scheduled_for=slot,
            published_at=None,
        )
    )
    db.commit()

    resp = client.get("/api/v1/calendar", headers=auth)

    assert resp.status_code == 200
    (entry,) = resp.json()["entries"]
    assert entry["title"] == "Live, timestamp pending"
    assert entry["movable"] is False
