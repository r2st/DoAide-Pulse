"""Scheduling for "09:00 local" rather than for an instant somebody computed.

Everything Pulse stores is UTC and that is not in question here. The question
is what a client is allowed to *send*, and the answer used to be: an instant, or
a naive wall-clock time that Pulse would read as UTC. Neither expresses the
thing a person setting a date actually means.

A browser can resolve "09:00 on 5 November in Berlin" to an offset — but it
resolves it *today*, against today's offset, and Berlin is +02:00 in August and
+01:00 in November. So a piece scheduled in August for a Thursday in November
goes out at 10:00 local, an hour late, and the only visible symptom is a post
that landed an hour after it said it would. Sending the zone name instead moves
the conversion to the date being asked about, which is the only place it can be
done correctly.

The three interesting hours of the year are here: an ordinary one, the one that
happens twice, and the one that does not happen at all.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import scheduling
from app.services.crypto import encrypt_credentials

#: Dates chosen from the clock rather than written down, because the ones that
#: were written down rotted: a literal September went into the past and the
#: reschedule tests started failing on the calendar alone. Every candidate has
#: to sit inside ``scheduling``'s window — in the future, and no more than 365
#: days out — so each helper picks the nearest instance of the date it needs
#: that is at least two days ahead.
_TODAY = datetime.now(UTC).date()


def _upcoming(*candidates: tuple[int, int]) -> datetime:
    """The nearest of *candidates* (month, day) at least two days ahead."""
    options = [
        datetime(_TODAY.year + years, month, day)
        for years in (0, 1)
        for month, day in candidates
    ]
    ahead = [d for d in options if (d.date() - _TODAY).days >= 2]
    return min(ahead)


def _second_sunday_of_march(year: int) -> datetime:
    first = datetime(year, 3, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7)


def _last_sunday_of_march(year: int) -> datetime:
    last = datetime(year, 3, 31)
    return last - timedelta(days=(last.weekday() + 1) % 7)


#: A morning a few weeks out, with a UTC offset of exactly one hour in Berlin:
#: mid-January or mid-February is standard time whatever the year, and one of
#: the two is always inside the window.
_WINTER = _upcoming((1, 15), (2, 15))
WINTER_MORNING = _WINTER.strftime("%Y-%m-%dT09:00:00")
WINTER_MORNING_UTC = _WINTER.strftime("%Y-%m-%dT08:00")
WINTER_INSTANT = _WINTER.replace(hour=8, tzinfo=UTC)
#: Any instant inside the window, for a schedule the test then moves.
SEPTEMBER_MORNING_Z = (datetime.now(UTC) + timedelta(days=20)).strftime(
    "%Y-%m-%dT09:00:00Z"
)
#: The hour that does not exist: 02:30 on the morning a zone springs forward.
#: America/New_York's is the second Sunday of March; from the two days before
#: it the next one is a year out, past the horizon, so Berlin's — the last
#: Sunday, two weeks later — stands in.
_gaps = [
    (_second_sunday_of_march(_TODAY.year + years), "America/New_York")
    for years in (0, 1)
] + [(_last_sunday_of_march(_TODAY.year + years), "Europe/Berlin") for years in (0, 1)]
_GAP, GAP_ZONE = min(
    (g for g in _gaps if 2 <= (g[0].date() - _TODAY).days <= 360), key=lambda g: g[0]
)
SPRING_FORWARD_GAP = _GAP.strftime("%Y-%m-%dT02:30:00")


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Timezones land",
        slug="timezones-land",
        body_markdown="Pulse reads a wall clock in the zone you meant.",
        excerpt="Pulse reads a wall clock in the zone you meant.",
        status=ContentStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def devto(db, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()


# --------------------------------------------------------------------------- #
# The conversion itself                                                        #
# --------------------------------------------------------------------------- #


def test_a_wall_clock_time_is_read_in_the_zone_that_was_named():
    """The base case, and the one the whole feature is for.

    09:00 in Berlin in November is 08:00 UTC. Without the zone the same string
    would be stored as 09:00 UTC and published an hour early.
    """
    when = scheduling.normalize(
        datetime(2026, 11, 5, 9, 0),
        tz="Europe/Berlin",
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert when == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def test_the_same_wall_clock_is_a_different_instant_in_summer():
    """Berlin is +02:00 in August and +01:00 in November.

    This is the pair a client-side offset gets wrong: whichever of the two is
    true on the day the form is submitted is applied to a date on the other side
    of the transition. The zone name carries the date-dependence with it.
    """
    summer = scheduling.normalize(
        datetime(2026, 8, 20, 9, 0),
        tz="Europe/Berlin",
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    winter = scheduling.normalize(
        datetime(2026, 11, 5, 9, 0),
        tz="Europe/Berlin",
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert summer.hour == 7
    assert winter.hour == 8


def test_a_timestamp_that_already_carries_an_offset_ignores_the_zone():
    """An instant is an instant.

    A client that computed ``+05:30`` has answered the question the zone
    parameter exists to ask, and letting the zone override it would silently
    move a time the caller had already pinned down. The value is still
    *converted* to UTC — that part is not new.
    """
    when = scheduling.normalize(
        datetime(2026, 9, 1, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        tz="America/New_York",
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert when == datetime(2026, 9, 1, 3, 30, tzinfo=UTC)


def test_the_hour_that_happens_twice_takes_the_earlier_one():
    """01:30 on the autumn transition in New York is two real instants.

    Both readings are correct, so neither is an error. The earlier is taken —
    still on summer time, 05:30 UTC rather than 06:30 — because a scheduler
    forced to choose should publish on time rather than an hour late.
    """
    when = scheduling.normalize(
        datetime(2026, 11, 1, 1, 30),
        tz="America/New_York",
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert when == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_the_hour_that_does_not_happen_is_refused():
    """02:30 on the spring transition in New York never occurs.

    :pep:`495` would quietly convert it using the pre-jump offset, landing the
    piece at 03:30 local — an hour after the person asked for, on the one date
    they are least likely to re-check. There is no correct instant to
    substitute, so the request is refused and says why.
    """
    with pytest.raises(scheduling.ScheduleError) as exc:
        scheduling.normalize(
            datetime(2026, 3, 8, 2, 30),
            tz="America/New_York",
            now=datetime(2026, 1, 15, tzinfo=UTC),
        )
    assert "does not exist" in str(exc.value)
    assert "America/New_York" in str(exc.value)


def test_an_hour_either_side_of_the_gap_is_fine():
    """The refusal above is one hour wide, not a whole day."""
    for hour, expected_utc in ((1, 6), (3, 7)):
        when = scheduling.normalize(
            datetime(2026, 3, 8, hour, 30),
            tz="America/New_York",
            now=datetime(2026, 1, 15, tzinfo=UTC),
        )
        assert when == datetime(2026, 3, 8, expected_utc, 30, tzinfo=UTC)


def test_a_zone_nobody_has_heard_of_is_refused_in_the_users_words():
    """Three different exceptions come out of zoneinfo for the same mistake.

    A missing key, a key that escapes TZPATH and a key type it will not touch
    raise ``ZoneInfoNotFoundError``, ``ValueError`` and ``KeyError``
    respectively. The router above turns exactly one exception type into a 422,
    so all three arrive as ``ScheduleError``.
    """
    for name in ("Mars/Olympus_Mons", "../../etc/passwd", "Europe/Berlin\x00"):
        with pytest.raises(scheduling.ScheduleError):
            scheduling.resolve_zone(name)


def test_an_absurdly_long_zone_name_is_refused_before_the_filesystem_is_walked():
    """Every miss is a walk of every entry in TZPATH.

    The longest real key is 31 characters. Refusing at twice that is not a
    guess about names, it is a refusal to go looking for one nobody typed.
    """
    with pytest.raises(scheduling.ScheduleError):
        scheduling.resolve_zone("A" * (scheduling.TIMEZONE_MAX_LENGTH + 1))


def test_no_zone_still_means_utc():
    """The old behaviour, unchanged, because every existing caller relies on it."""
    assert scheduling.normalize(
        datetime(2026, 9, 1, 9, 0), now=datetime(2026, 8, 15, tzinfo=UTC)
    ) == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def test_an_empty_zone_is_the_same_as_no_zone():
    """A form that always sends the field sends "" when nobody filled it in."""
    assert scheduling.resolve_zone("") is None
    assert scheduling.resolve_zone("   ") is None
    assert scheduling.normalize(
        datetime(2026, 9, 1, 9, 0), tz="", now=datetime(2026, 8, 15, tzinfo=UTC)
    ) == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def test_a_zone_object_passes_through():
    """Callers that already hold a ZoneInfo need not spell its name back out."""
    zone = ZoneInfo("Europe/Berlin")
    assert scheduling.resolve_zone(zone) is zone


def test_clearing_a_schedule_still_validates_the_zone():
    """``scheduled_for: null`` with a bad zone is a client bug worth reporting.

    Answering 200 would teach the caller that the name was accepted, and the
    next request — the one that carries a time — would be the first to fail.
    """
    with pytest.raises(scheduling.ScheduleError):
        scheduling.normalize(None, tz="Mars/Olympus_Mons")
    assert scheduling.normalize(None, tz="Europe/Berlin") is None


# --------------------------------------------------------------------------- #
# The checks run on the instant, not on the wall clock                         #
# --------------------------------------------------------------------------- #


def test_the_past_check_is_made_on_the_instant_not_the_wall_clock():
    """09:00 in Auckland can be in the past while 09:00 in Los Angeles is not.

    "In the past" has exactly one meaning a worker will agree with, and it is
    the one measured in the units the sweep runs in.
    """
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # 06:00 in Los Angeles on the same day is 13:00 UTC — an hour ahead.
    assert scheduling.normalize(
        datetime(2026, 6, 1, 6, 0), tz="America/Los_Angeles", now=now
    ) == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # The identical wall clock in Auckland is 18:00 UTC the day *before*.
    with pytest.raises(scheduling.ScheduleError) as exc:
        scheduling.normalize(datetime(2026, 6, 1, 6, 0), tz="Pacific/Auckland", now=now)
    assert "in the past" in str(exc.value)


def test_the_horizon_check_is_made_on_the_instant_too():
    """A zone cannot buy a caller a day past the horizon, or cost them one."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    far = now + timedelta(days=400)
    with pytest.raises(scheduling.ScheduleError) as exc:
        scheduling.normalize(
            far.replace(tzinfo=None), tz="Europe/Berlin", now=now
        )
    assert "check the year" in str(exc.value)


# --------------------------------------------------------------------------- #
# The endpoints that take one                                                  #
# --------------------------------------------------------------------------- #


def test_the_publish_endpoint_reads_a_wall_clock_in_the_zone_it_is_given(
    client, auth, db, piece, devto
):
    """POST /content/{id}/publish, which is where a scheduled publish starts."""
    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={
            "platforms": ["devto"],
            "scheduled_for": WINTER_MORNING,
            "timezone": "Europe/Berlin",
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    row = db.query(Publication).filter_by(content_id=piece.id).one()
    assert row.status == PublicationStatus.SCHEDULED
    assert row.scheduled_for.replace(tzinfo=UTC) == WINTER_INSTANT
    # And the response says the same thing, so a client need not re-read.
    assert resp.json()[0]["scheduled_for"].startswith(WINTER_MORNING_UTC)


def test_the_publish_endpoint_refuses_a_zone_it_cannot_resolve(
    client, auth, piece, devto
):
    """A typo'd zone is a 422 — the same answer a time in the past gets, and for
    the same reason: the caller has named a moment Pulse cannot act on."""
    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={
            "platforms": ["devto"],
            "scheduled_for": WINTER_MORNING,
            "timezone": "Europe/Berlyn",
        },
        headers=auth,
    )
    assert resp.status_code == 422
    assert "Europe/Berlyn" in resp.json()["detail"]


def test_an_over_long_zone_is_refused_by_the_schema_before_the_route_runs(
    client, auth, piece, devto
):
    """Two bounds, and this is the free one: pydantic refuses the string before
    any route body is entered."""
    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={
            "platforms": ["devto"],
            "scheduled_for": WINTER_MORNING,
            "timezone": "A" * 500,
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_the_calendar_reschedule_takes_a_zone(client, auth, db, piece, devto):
    """Drag-and-drop is where this matters most: the calendar draws local days,
    so a card dropped on "Tuesday 09:00" means 09:00 in the user's own week."""
    client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={"platforms": ["devto"], "scheduled_for": SEPTEMBER_MORNING_Z},
        headers=auth,
    )
    resp = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": WINTER_MORNING, "timezone": "Europe/Berlin"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    row = db.query(Publication).filter_by(content_id=piece.id).one()
    assert row.scheduled_for.replace(tzinfo=UTC) == WINTER_INSTANT


def test_the_calendar_reschedule_refuses_an_impossible_local_time(
    client, auth, piece, devto
):
    """The gap reaches the drag-and-drop surface as a 422 rather than a silent
    hour of drift."""
    client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={"platforms": ["devto"], "scheduled_for": SEPTEMBER_MORNING_Z},
        headers=auth,
    )
    resp = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": SPRING_FORWARD_GAP, "timezone": GAP_ZONE},
        headers=auth,
    )
    assert resp.status_code == 422
    assert "does not exist" in resp.json()["detail"]


def test_a_bulk_publish_applies_one_zone_to_the_whole_batch(
    client, auth, db, project, devto
):
    """``timezone`` is a property of the request, as ``scheduled_for`` is.

    A batch is one instant for every piece in it; staggering is what the
    calendar's reschedule is for.
    """
    ids = []
    for n in range(2):
        row = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            title=f"Batch {n}",
            slug=f"batch-{n}",
            body_markdown="Body.",
            excerpt="Body.",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        ids.append(row.id)

    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={
            "content_ids": ids,
            "platforms": ["devto"],
            "scheduled_for": WINTER_MORNING,
            "timezone": "Europe/Berlin",
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["succeeded"]) == sorted(ids)
    for content_id in ids:
        row = db.query(Publication).filter_by(content_id=content_id).one()
        assert row.scheduled_for.replace(tzinfo=UTC) == WINTER_INSTANT
