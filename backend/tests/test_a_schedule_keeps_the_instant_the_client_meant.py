"""``scheduled_for`` is an instant, and an offset is not decoration.

:func:`app.services.scheduling.normalize` is the one gate every writer of
``scheduled_for`` goes through — ``POST /content/{id}/publish``, ``POST
/content/{id}/schedule``, ``PATCH /content/{id}`` and the calendar's
drag-and-drop all call it — and its module docstring ends "Everything here is
UTC. The browser renders local time; the database never sees one."

It did see one. ``as_aware`` *labels* a naive datetime UTC and leaves an aware
one exactly as it arrived, so ``2026-09-01T09:00+05:30`` was validated as an
instant (correctly) and then returned, stored and echoed in the offset the
client happened to be in. Two things follow, and the first is why this went
unnoticed for so long:

* **On SQLite the offset is dropped.** ``DateTime(timezone=True)`` is a real
  ``timestamptz`` on PostgreSQL, which normalises on the way in, so production
  stored the right instant. SQLite's DATETIME writes the wall clock and nothing
  else — ``09:00+05:30`` goes in and ``09:00`` comes out, which ``as_aware``
  then reads back as ``09:00Z``. The whole suite runs on SQLite, so every
  scheduling test written with an offset agreed with the bug by construction.
  That is the case pinned below against the database rather than against the
  function.

* **On both, the value goes back out in the wrong frame.** ``scheduled_for`` is
  serialised beside ``created_at``, ``updated_at`` and ``published_at``, all of
  which are UTC because :func:`app.models.mixins.utcnow` wrote them. A client
  differencing two of them got a number wrong by the offset.

A daylight-saving transition is the case where the offset is not an alternative
spelling of the same thing. On the US fall-back night, 01:30 happens twice in
New York: once at ``-04:00`` and once, an hour later, at ``-05:00``. The wall
clock cannot tell them apart and the offset is the only thing that can, which is
exactly what a client scheduling into that hour is relying on.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import scheduling

#: The two sides of a US daylight-saving transition. Eastern is the zone
#: Herald's cadence table is written against, so it is the one a user is most
#: likely to be dragging a calendar item around in.
EDT = timezone(-timedelta(hours=4))
EST = timezone(-timedelta(hours=5))
#: A zone with a half-hour offset, because an hour-aligned one hides a whole
#: class of arithmetic error: 09:00+01:00 and 08:00Z differ by a round number
#: that several wrong implementations also produce.
IST = timezone(timedelta(hours=5, minutes=30))

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Scheduling across a timezone",
        slug="scheduling-across-a-timezone",
        body_markdown="The body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# normalize returns UTC                                                        #
# --------------------------------------------------------------------------- #


def test_an_offset_time_comes_back_in_utc():
    when = datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    normalized = scheduling.normalize(when, now=NOW)
    assert normalized.tzinfo is UTC
    assert normalized == datetime(2026, 9, 1, 3, 30, tzinfo=UTC)


def test_converting_does_not_move_the_instant():
    """The frame changes; the moment does not. That is the whole guarantee."""
    when = datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    assert scheduling.normalize(when, now=NOW) == when


def test_a_naive_time_is_still_read_as_utc():
    """Unchanged: the lenient reading the docstring argues for is still there."""
    normalized = scheduling.normalize(datetime(2026, 9, 1, 9, 0), now=NOW)
    assert normalized == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert normalized.tzinfo is UTC


def test_a_time_already_in_utc_is_returned_unchanged():
    when = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert scheduling.normalize(when, now=NOW) == when


def test_none_still_passes_through():
    assert scheduling.normalize(None, now=NOW) is None


# --------------------------------------------------------------------------- #
# The DST hour that happens twice                                              #
# --------------------------------------------------------------------------- #


def test_the_two_halves_of_a_repeated_hour_stay_an_hour_apart():
    """01:30 EDT and 01:30 EST are the same wall clock and different instants."""
    first = scheduling.normalize(datetime(2026, 11, 1, 1, 30, tzinfo=EDT), now=NOW)
    second = scheduling.normalize(datetime(2026, 11, 1, 1, 30, tzinfo=EST), now=NOW)

    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert second - first == timedelta(hours=1)


def test_a_wall_time_that_does_not_exist_locally_is_still_an_instant():
    """02:30 on the spring-forward morning never happens in New York.

    It is still a perfectly good instant when the client says which side of the
    jump it meant, and refusing it would be Herald having an opinion about a
    calendar it does not own. UTC has no transitions, so there is nothing here
    to disambiguate once the offset has been applied.
    """
    when = datetime(2027, 3, 14, 2, 30, tzinfo=EST)
    assert scheduling.normalize(when, now=NOW) == datetime(2027, 3, 14, 7, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The past and horizon checks measure instants                                 #
# --------------------------------------------------------------------------- #


def test_a_wall_clock_behind_now_is_accepted_when_the_instant_is_ahead():
    """08:00-05:00 reads as "this morning" and is an hour from now.

    The refusal is about the moment, not about how the moment is spelled.
    """
    when = datetime(2026, 8, 15, 8, 0, tzinfo=EST)  # 13:00 UTC
    assert scheduling.normalize(when, now=NOW) == datetime(2026, 8, 15, 13, 0, tzinfo=UTC)


def test_a_wall_clock_ahead_of_now_is_refused_when_the_instant_is_behind():
    when = datetime(2026, 8, 15, 16, 0, tzinfo=IST)  # 10:30 UTC, ninety minutes ago
    with pytest.raises(scheduling.ScheduleError):
        scheduling.normalize(when, now=NOW)


def test_the_refusal_message_names_the_instant_in_utc():
    """A client shown its own offset back cannot tell why the time was refused."""
    when = datetime(2026, 8, 15, 16, 0, tzinfo=IST)
    with pytest.raises(scheduling.ScheduleError) as exc:
        scheduling.normalize(when, now=NOW)
    assert "+00:00" in str(exc.value)


# --------------------------------------------------------------------------- #
# The value that reaches the database                                          #
# --------------------------------------------------------------------------- #


def test_the_stored_row_holds_the_instant_not_the_wall_clock(db, piece):
    """The SQLite case, which is what made this invisible.

    Storing the un-normalised value and reading it back is a five-and-a-half
    hour move. This is the assertion that fails against the old ``normalize``
    on the database the suite actually runs on.
    """
    when = scheduling.normalize(datetime(2026, 9, 1, 9, 0, tzinfo=IST), now=NOW)
    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=when,
    )
    db.add(publication)
    db.commit()
    db.expire_all()

    stored = as_aware(db.get(Publication, publication.id).scheduled_for)
    assert stored == datetime(2026, 9, 1, 3, 30, tzinfo=UTC)


def test_the_two_halves_of_a_repeated_hour_survive_a_round_trip(db, piece):
    """And the DST pair is still an hour apart once it has been through storage."""
    rows = []
    for platform, tz in ((Platform.DEVTO, EDT), (Platform.MEDIUM, EST)):
        when = scheduling.normalize(datetime(2026, 11, 1, 1, 30, tzinfo=tz), now=NOW)
        row = Publication(
            content_id=piece.id,
            platform=platform,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=when,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    db.expire_all()

    first, second = (as_aware(db.get(Publication, r.id).scheduled_for) for r in rows)
    assert second - first == timedelta(hours=1)


# --------------------------------------------------------------------------- #
# Through the API                                                              #
# --------------------------------------------------------------------------- #


#: A time comfortably inside the horizon, expressed at ``+05:30`` — 03:30 UTC.
#: A month out rather than a literal date: the literal rotted into the past and
#: the three tests below started failing on the calendar, not on a change.
_OFFSET_DAY = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%d")
OFFSET_TIME = f"{_OFFSET_DAY}T09:00:00+05:30"
OFFSET_TIME_UTC = f"{_OFFSET_DAY}T03:30:00"


def test_patching_a_schedule_answers_in_utc(client, auth, piece):
    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        json={"scheduled_for": OFFSET_TIME},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scheduled_for"].startswith(OFFSET_TIME_UTC)


def test_rescheduling_from_the_calendar_answers_in_utc(
    client, auth, db, piece, connect
):
    connect(Platform.DEVTO)
    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
    )
    db.add(publication)
    db.commit()

    resp = client.patch(
        f"/api/v1/calendar/content/{piece.id}",
        json={"scheduled_for": OFFSET_TIME},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["scheduled_for"].startswith(OFFSET_TIME_UTC)


def test_the_piece_and_its_publication_agree_after_a_patch(client, auth, db, piece, connect):
    """One instant, written to two columns by two code paths in one request."""
    connect(Platform.DEVTO)
    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
    )
    db.add(publication)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        json={"scheduled_for": OFFSET_TIME},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()

    expected = datetime.fromisoformat(OFFSET_TIME_UTC).replace(tzinfo=UTC)
    assert as_aware(db.get(Content, piece.id).scheduled_for) == expected
    assert as_aware(db.get(Publication, publication.id).scheduled_for) == expected
