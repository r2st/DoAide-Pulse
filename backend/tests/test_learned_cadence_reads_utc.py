"""The hour a cadence is learned at is the hour a scheduler will publish at.

:mod:`app.services.learned_cadence` reduces a published post to
``published_at.hour`` and hands the winner to a scheduler whose field is called
``best_hour_utc``. So the read has to be UTC, and it was only *incidentally* UTC:
the code called ``as_aware``, which labels a naive value and leaves an aware one
exactly as it found it — offset and all.

Nothing produced a shifted value on purpose. ``published_at`` is written by
``utcnow()``, and Herald pins the database session's zone to UTC at connect time
(``app.database._connect_options``, added when this was found). But that is one
libpq parameter standing between a correct publishing rhythm and one silently
off by the server's offset, on a path where being wrong is invisible: a learned
09:00 that is really 09:00 in Berlin looks exactly like a learned 09:00, and
the rationale string underneath it says "your best start" either way.

So the field is converted where it is read, the way
``analytics_service.utc_day`` does it where the chart buckets are built.
``Curve`` objects are built by hand here rather than round-tripped through the
database, because the database in the tests is SQLite — it has no time zones and
hands back the naive UTC that was written, which is why this bug only ever
existed where it was expensive.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.config import settings
from app.models.publication import Platform
from app.services import learned_cadence, velocity

#: Far enough east that a post published mid-evening UTC is already tomorrow —
#: which moves the weekday as well as the hour, and the rota is learned from the
#: weekday.
_TOKYO = timezone(timedelta(hours=9))


def _curve(published_at: datetime, *, views: int, publication_id: int) -> velocity.Curve:
    """One post with a single reading covering the early window.

    The reading sits *at* the window rather than halfway through it:
    ``Curve._covering`` refuses a point too early to stand for the window it is
    asked about, and would answer ``None`` — which ``observations`` correctly
    drops, leaving nothing for these tests to be about.
    """
    window = float(settings.velocity_early_window_hours)
    return velocity.Curve(
        publication_id=publication_id,
        content_id=publication_id,
        platform=Platform.DEVTO,
        title=f"Post {publication_id}",
        published_at=published_at,
        age_hours=window * 4,
        points=[velocity.Point(hours=window, views=views, engagement=0)],
        observed_hours=window,
    )


def _observed(curves: list[velocity.Curve]) -> list[tuple[int, int]]:
    """``(hour, weekday)`` per curve, as the learner sees them."""
    return [
        (o.hour, o.weekday)
        for o in learned_cadence.observations(
            None, 1, Platform.DEVTO, known=curves  # type: ignore[arg-type]
        )
    ]


def test_a_post_read_back_on_another_clock_is_still_read_as_utc():
    """2026-08-14 22:00 UTC is a Friday evening, and 07:00 Saturday in Tokyo."""
    instant = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    assert instant.hour == 22 and instant.weekday() == 4  # Friday

    shifted = instant.astimezone(_TOKYO)
    # The premise: this is the same moment wearing a different face, and
    # ``as_aware`` alone would have handed both of those fields straight through.
    assert (shifted.hour, shifted.weekday()) == (7, 5)

    assert _observed([_curve(shifted, views=100, publication_id=1)]) == [(22, 4)]


def test_a_naive_timestamp_is_still_treated_as_utc():
    """What SQLite returns, and what ``as_aware`` was there for in the first place."""
    naive = datetime(2026, 8, 14, 22, 0)

    assert _observed([_curve(naive, views=100, publication_id=1)]) == [(22, 4)]


def test_the_learned_hour_is_the_utc_hour_not_the_offset_one():
    """End to end, on the number that reaches the scheduler.

    Every post goes out at the same instant and does well, so there is exactly
    one hour bucket and it wins. Read on a Tokyo clock the answer would be 07:00
    — seven hours out, every week, with no way for the user to tell.
    """
    instant = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    curves = [
        _curve(
            (instant - timedelta(days=7 * n)).astimezone(_TOKYO),
            views=500,
            publication_id=n + 1,
        )
        for n in range(max(settings.learned_cadence_min_samples, 5))
    ]

    learned = learned_cadence.learn(None, 1, Platform.DEVTO, known=curves)  # type: ignore[arg-type]

    assert learned.is_learned
    assert learned.cadence.best_hour_utc == 22
    assert learned.as_dict()["best_time_utc"] == "22:00"


def test_the_learned_weekday_is_the_utc_weekday_too():
    """The rota shifts with the hour — 22:00 Friday UTC is Saturday in Tokyo.

    A user told to post on Saturdays because their Friday posts do well is being
    given the wrong day by the same one-line read.
    """
    instant = datetime(2026, 8, 14, 22, 0, tzinfo=UTC)
    curves = [
        _curve(
            (instant - timedelta(days=7 * n)).astimezone(_TOKYO),
            views=500,
            publication_id=n + 1,
        )
        for n in range(max(settings.learned_cadence_min_samples, 5))
    ]

    learned = learned_cadence.learn(None, 1, Platform.DEVTO, known=curves)  # type: ignore[arg-type]

    assert learned.weekdays_learned
    assert learned.cadence.best_weekdays == (4,)  # Friday, not Saturday
    assert learned.as_dict()["best_weekdays"] == ["Fri"]
