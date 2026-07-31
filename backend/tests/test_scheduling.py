"""Scheduling: normalising a requested time, picking one, and the endpoints.

The failure this file mostly guards against is silent: a time in the past is
picked up by the very next sweep, so "schedule for 2025" when you meant 2026
publishes immediately and looks like a bug in the publisher rather than a typo
in the form.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import scheduling
from app.services.crypto import encrypt_credentials


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Scheduling lands",
        slug="scheduling-lands",
        body_markdown="Herald can now put a piece on the calendar.",
        excerpt="Herald can now put a piece on the calendar.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def devto_connected(db, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
            display_name="@dev",
        )
    )
    db.commit()


# --------------------------------------------------------------------------- #
# normalize                                                                   #
# --------------------------------------------------------------------------- #


def test_none_means_publish_now_and_survives():
    assert scheduling.normalize(None) is None


def test_a_naive_time_is_read_as_utc():
    """Clients that hand-assemble "2026-08-04T09:00" are common enough."""
    when = datetime(2099, 1, 1, 9, 0)  # naive
    normalized = scheduling.normalize(when, now=datetime(2098, 12, 1, tzinfo=UTC))
    assert normalized.tzinfo is not None
    assert normalized == datetime(2099, 1, 1, 9, 0, tzinfo=UTC)


def test_the_past_is_refused_rather_than_published_immediately():
    with pytest.raises(scheduling.ScheduleError, match="in the past"):
        scheduling.normalize(_now() - timedelta(days=1))


def test_a_small_clock_skew_is_forgiven():
    """A client a minute behind the server should not get an error."""
    skew = timedelta(seconds=settings.schedule_past_grace_seconds - 10)
    assert scheduling.normalize(_now() - skew) is not None


def test_beyond_the_horizon_is_refused():
    too_far = _now() + timedelta(days=settings.schedule_max_horizon_days + 1)
    with pytest.raises(scheduling.ScheduleError, match="check the year"):
        scheduling.normalize(too_far)


def test_inside_the_horizon_passes():
    ok = _now() + timedelta(days=settings.schedule_max_horizon_days - 1)
    assert scheduling.normalize(ok) is not None


# --------------------------------------------------------------------------- #
# Choosing a slot                                                             #
# --------------------------------------------------------------------------- #


def test_a_slot_lands_on_a_good_day_and_hour_for_the_platform(db, user):
    from app.services.cadence import cadence_for

    slots = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])
    assert len(slots) == 1

    cadence = cadence_for(Platform.DEVTO)
    assert slots[0].when.weekday() in cadence.best_weekdays
    assert slots[0].when.hour == cadence.best_hour_utc
    assert slots[0].when > _now()
    assert slots[0].rationale


def test_a_cross_post_staggers_rather_than_firing_at_once(db, user):
    slots = scheduling.optimal_slots(
        db, user.id, [Platform.DEVTO, Platform.MEDIUM, Platform.MASTODON]
    )
    times = [slot.when for slot in slots]

    assert len(set(times)) == 3
    assert times == sorted(times)


def test_copies_are_ordered_behind_the_canonical_platform(db, user):
    """A copy published before the original has no URL to be canonical to."""
    slots = scheduling.optimal_slots(
        db,
        user.id,
        [Platform.MEDIUM, Platform.DEVTO],
        canonical=Platform.DEVTO,
    )
    by_platform = {slot.platform: slot.when for slot in slots}

    assert by_platform[Platform.DEVTO] < by_platform[Platform.MEDIUM]
    assert by_platform[Platform.MEDIUM] - by_platform[Platform.DEVTO] >= timedelta(
        seconds=settings.syndication_delay_seconds
    )


def test_a_slot_avoids_something_already_scheduled(db, user, project, piece):
    """Otherwise "schedule the next three" stacks them on one Tuesday."""
    first = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])[0]
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=first.when,
        )
    )
    db.commit()

    second = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])[0]
    assert second.when != first.when
    assert abs((second.when - first.when).total_seconds()) >= 12 * 3600


def test_taken_slots_ignores_the_past_and_the_finished(db, user, project, piece):
    db.add_all(
        [
            Publication(
                content_id=piece.id,
                platform=Platform.DEVTO,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=_now() + timedelta(days=3),
            ),
            Publication(
                content_id=piece.id,
                platform=Platform.MEDIUM,
                status=PublicationStatus.PUBLISHED,
                scheduled_for=_now() + timedelta(days=4),
            ),
        ]
    )
    db.commit()

    taken = scheduling.taken_slots(db, user.id)
    assert len(taken) == 1


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def test_publishing_in_the_past_is_refused(client, auth, piece, devto_connected):
    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={
            "platforms": ["devto"],
            "scheduled_for": (_now() - timedelta(days=2)).isoformat(),
        },
    )
    assert resp.status_code == 422
    assert "in the past" in resp.json()["detail"]


def test_schedule_puts_a_piece_on_the_calendar(client, auth, db, piece, devto_connected):
    when = _now() + timedelta(days=3)
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": when.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body) == 1
    assert body[0]["status"] == "scheduled"
    db.refresh(piece)
    assert piece.status == ContentStatus.APPROVED
    assert abs(as_aware(piece.scheduled_for) - when) < timedelta(seconds=1)


def test_schedule_can_pick_the_time_for_you(client, auth, db, piece, devto_connected):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto"], "optimize": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body[0]["status"] == "scheduled"
    when = datetime.fromisoformat(body[0]["scheduled_for"])
    assert as_aware(when) > _now()


def test_a_time_and_optimize_together_is_refused(client, auth, piece, devto_connected):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={
            "platforms": ["devto"],
            "optimize": True,
            "scheduled_for": (_now() + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 400
    assert "not both" in resp.json()["detail"]


def test_neither_a_time_nor_optimize_is_refused(client, auth, piece, devto_connected):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto"]},
    )
    assert resp.status_code == 400
    assert "publish" in resp.json()["detail"]


def test_scheduling_nowhere_says_so(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"optimize": True},
    )
    assert resp.status_code == 400
    assert "not queued anywhere" in resp.json()["detail"]


def test_rescheduling_reuses_the_platforms_already_queued(
    client, auth, db, piece, devto_connected
):
    client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": (_now() + timedelta(days=1)).isoformat()},
    )

    moved = _now() + timedelta(days=5)
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"scheduled_for": moved.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    # Re-armed, not duplicated.
    assert db.query(Publication).filter_by(content_id=piece.id).count() == 1


def test_scheduling_an_unconnected_platform_fails_now_not_at_3am(
    client, auth, piece
):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={
            "platforms": ["devto"],
            "scheduled_for": (_now() + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 400
    assert "Not connected" in resp.json()["detail"]


def test_suggestions_propose_without_changing_anything(
    client, auth, db, piece, devto_connected
):
    resp = client.get(
        f"/api/v1/content/{piece.id}/schedule/suggestions?platforms=devto&platforms=medium",
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert {row["platform"] for row in body} == {"devto", "medium"}
    assert all(row["rationale"] for row in body)
    # Chronological, which is also the order they would be dispatched in.
    assert [row["when"] for row in body] == sorted(row["when"] for row in body)
    # Nothing was queued.
    assert db.query(Publication).count() == 0


def test_unschedule_takes_it_off_the_calendar(client, auth, db, piece, devto_connected):
    client.post(
        f"/api/v1/content/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": (_now() + timedelta(days=2)).isoformat()},
    )

    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["status"] == "cancelled"

    db.refresh(piece)
    assert piece.scheduled_for is None
    publication = db.query(Publication).filter_by(content_id=piece.id).one()
    # Cancelled, not pending — pending means "go out on the next sweep", which
    # is the opposite of what unschedule asked for.
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.scheduled_for is None


def test_unscheduling_nothing_is_not_an_error(client, auth, piece):
    resp = client.delete(f"/api/v1/content/{piece.id}/schedule", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == []


def test_schedule_endpoints_are_scoped_to_the_owner(client, auth, db, user):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.flush()
    other = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.flush()
    theirs = Content(
        project_id=other.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Theirs",
        slug="theirs-post",
    )
    db.add(theirs)
    db.commit()

    assert (
        client.get(
            f"/api/v1/content/{theirs.id}/schedule/suggestions", headers=auth
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/content/{theirs.id}/schedule", headers=auth).status_code
        == 404
    )
