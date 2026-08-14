"""The first day of a daily chart is a whole day.

Both daily series — :func:`timeline` and :func:`engagement_trend` — label their
buckets by date and then filter on ``captured_at >= since``. While ``since`` was
``utcnow() - timedelta(days=days)`` those two things disagreed: the bucket was
drawn for a whole calendar day and filled from only the part of it after the
current clock time. A dashboard opened at 21:00 charted its oldest day from
21:00 onwards and reported the other twenty-one hours as nothing having
happened.

What made it hard to see from the outside is that it moved. The shortfall is
however long the day has been running, so the same window over the same data
reported a different oldest day every hour, and each answer looked like a real
quiet spell rather than a boundary in the wrong place.

Everything is UTC — see :mod:`app.services.cadence`. Reproduced by writing rows
just after midnight UTC on the oldest charted day and asking for them back,
which is a fixed distance from the window's start and so does not depend on what
time the suite happens to run.
"""
from __future__ import annotations

from datetime import timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import analytics_service

DAYS = 30


def _first_charted_day():
    """Midnight UTC on the oldest day either series draws."""
    return (utcnow() - timedelta(days=DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _piece(db, project, title):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title=title,
        slug=title.lower().replace(" ", "-"),
        body_markdown="word " * 220,
        status=ContentStatus.PUBLISHED,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# window_start                                                                 #
# --------------------------------------------------------------------------- #


def test_window_start_is_midnight():
    start = analytics_service.window_start(DAYS)

    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.tzinfo is not None


def test_window_start_keeps_the_date_the_chart_already_labelled():
    """Flooring must not shift the axis, only fill the bucket it already drew."""
    assert (
        analytics_service.window_start(DAYS).date()
        == (utcnow() - timedelta(days=DAYS)).date()
    )


# --------------------------------------------------------------------------- #
# timeline                                                                     #
# --------------------------------------------------------------------------- #


def test_timeline_counts_a_publish_from_the_start_of_its_oldest_day(
    db, user, project
):
    """00:01 on the oldest charted day is inside the chart, so it must be counted."""
    day = _first_charted_day()
    piece = _piece(db, project, "Small hours")
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=day + timedelta(minutes=1),
        )
    )
    db.commit()

    rows = analytics_service.timeline(db, user.id, days=DAYS)

    assert rows[0]["date"] == day.date().isoformat()
    assert rows[0]["publications"] == 1


def test_timeline_still_covers_every_day_inclusive(db, user):
    rows = analytics_service.timeline(db, user.id, days=7)

    assert len(rows) == 8
    assert rows[0]["date"] == (utcnow() - timedelta(days=7)).date().isoformat()
    assert rows[-1]["date"] == utcnow().date().isoformat()


def test_timeline_does_not_reach_back_before_the_day_it_charts(db, user, project):
    """The floor widens the window by hours, not by a day."""
    piece = _piece(db, project, "Too old")
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=_first_charted_day() - timedelta(minutes=1),
        )
    )
    db.commit()

    rows = analytics_service.timeline(db, user.id, days=DAYS)

    assert sum(row["publications"] for row in rows) == 0


# --------------------------------------------------------------------------- #
# engagement_trend                                                             #
# --------------------------------------------------------------------------- #


def test_engagement_trend_measures_its_oldest_day_from_the_day_before(
    db, user, project
):
    """The baseline has to come from outside the day being charted.

    A gain is a subtraction against the publication's previous reading, and
    ``_pre_window_readings`` supplies "the last reading before the window". With
    the window starting mid-morning, that reading was itself inside the oldest
    charted day — so the morning's gain was subtracted away as part of the
    baseline and appeared in no bucket at all. Here: 100 views the day before,
    140 by 02:00, 180 by 23:00. The day's gain is 80.
    """
    day = _first_charted_day()
    piece = _piece(db, project, "Baseline")
    pub = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=day - timedelta(days=2),
    )
    db.add(pub)
    db.flush()
    for captured, views in (
        (day - timedelta(hours=3), 100),
        (day + timedelta(hours=2), 140),
        (day + timedelta(hours=23), 180),
    ):
        db.add(ContentMetric(publication_id=pub.id, captured_at=captured, views=views))
    db.commit()

    trend = {row["date"]: row for row in analytics_service.engagement_trend(
        db, user.id, days=DAYS
    )}

    assert trend[day.date().isoformat()]["views"] == 80


def test_engagement_trend_still_covers_every_day_inclusive(db, user):
    assert len(analytics_service.engagement_trend(db, user.id, days=7)) == 8
