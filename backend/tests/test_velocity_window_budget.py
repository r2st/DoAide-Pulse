"""The calendar must not get slower every month the account stays open.

``velocity.curves`` read every snapshot of every published publication. The
calendar builds those curves on every load, and everything it does with them —
``learned_cadence.learn`` for the suggested slots, ``describe_all`` for the
sidebar — asks one question: how many views did this post get in its first
``velocity_early_window_hours``. ``content_metrics`` is append-only and nothing
prunes it, so a post polled every six hours for a year carried some 1,400 rows
into memory to answer a question about its first day. Same shape as the weekly
digest's ``_gains`` (see :mod:`tests.test_digest_history_budget`), on a page
people leave open.

Three things are asserted, and all three are needed:

* **The numbers did not change.** A bounded read that gets a different answer is
  not an optimisation. The window is chosen so the reading that answers
  ``views_within`` is inside it; if the bound were off by one poll the medians
  behind every suggested time would shift.
* **The read is bounded.** Counted as ``ContentMetric`` rows reaching the
  session, because that is the thing that regressed — the *query* count was
  already two and stayed two, so a query budget would not have caught this and
  would not catch it coming back.
* **A truncated curve refuses the questions it cannot answer.** The failure mode
  is otherwise silent and plausible: a post with a year of history, read through
  its first day, looks exactly like a post that stopped growing after a day, and
  ``is_stalled`` would have said so.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.services import learned_cadence, velocity

#: Long enough that the unbounded read is unmistakably unbounded, short enough
#: that inserting the rows stays cheap.
_DAYS_LIVE = 400


@pytest.fixture
def long_running_publication(db, project):
    """One post live for over a year, polled four times on day one and daily since.

    The four early readings are the realistic shape — the default poll interval
    is six hours — and they are what the bounded query has to keep. Everything
    after them is the history the first-day question has no use for.
    """
    content = Content(
        project_id=project.id,
        title="A post that has been up for a year",
        slug="a-post-that-has-been-up-for-a-year",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    published_at = utcnow() - timedelta(days=_DAYS_LIVE)
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_url="https://dev.to/x/a-post-that-has-been-up-for-a-year",
    )
    db.add(publication)
    db.flush()

    for hours in (6, 12, 18, 24):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=published_at + timedelta(hours=hours),
                views=hours * 10,
                reactions=1,
            )
        )
    for day in range(2, _DAYS_LIVE):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=published_at + timedelta(days=day),
                views=240 + day * 5,
                reactions=1,
            )
        )
    db.commit()
    db.refresh(publication)
    return publication


@pytest.fixture
def metrics_read(db):
    """Every ``ContentMetric`` row that reaches the session while this is active.

    Counted through the ``loaded_as_persistent`` event rather than by inspecting
    the identity map afterwards, which is what
    :mod:`tests.test_digest_history_budget` does. The difference matters here:
    ``velocity`` reduces each snapshot to a :class:`~app.services.velocity.Point`
    and keeps no reference to the row, the identity map holds weak references,
    and so the rows this is meant to count have been collected by the time the
    assertion runs. The event fires as each row is loaded, which is the moment
    the cost is actually paid.
    """
    seen: list[int] = []

    def _record(session, instance):
        if isinstance(instance, ContentMetric):
            seen.append(instance.id)

    event.listen(db, "loaded_as_persistent", _record)
    try:
        yield seen
    finally:
        event.remove(db, "loaded_as_persistent", _record)


def _forget_everything(db, user):
    """Empty the identity map so the counts below are what the call loaded.

    A row already in the identity map is handed back without a fresh load, so
    without this a snapshot the fixture happened to touch would go uncounted.
    The user has to be fetched again afterwards: expunging detaches it, and a
    detached instance cannot answer ``user.id``.
    """
    user_id = user.id
    db.expunge_all()
    return db.get(User, user_id)


# ---- The answer ---------------------------------------------------------- #


def test_the_first_window_reads_the_same_whether_or_not_the_tail_was_fetched(
    db, user, long_running_publication
):
    """The bound must be an optimisation, not a different question.

    The +24h reading is the one ``views_within`` lands on either way — the
    bounded query has to include it, not stop at the poll before it.
    """
    early = float(settings.velocity_early_window_hours)

    (whole,) = velocity.curves(db, user.id)
    (bounded,) = velocity.curves(db, user.id, within_hours=early)

    assert whole.views_within(early) == 240
    assert bounded.views_within(early) == whole.views_within(early)


def test_the_cadence_learned_from_a_bounded_read_is_the_cadence_learned_from_all_of_it(
    db, user, long_running_publication
):
    """The only consumer of these curves has to agree with itself."""
    early = float(settings.velocity_early_window_hours)

    from_whole = learned_cadence.learn(
        db, user.id, Platform.DEVTO, known=velocity.curves(db, user.id)
    )
    from_bounded = learned_cadence.learn(
        db,
        user.id,
        Platform.DEVTO,
        known=velocity.curves(db, user.id, within_hours=early),
    )

    assert from_bounded.as_dict() == from_whole.as_dict()


# ---- The bound ----------------------------------------------------------- #


def test_a_year_of_snapshots_is_not_read_to_answer_for_a_first_day(
    db, user, long_running_publication, metrics_read
):
    """The bound: four rows, not one per day since the post went up."""
    assert db.query(ContentMetric).count() > 400
    user = _forget_everything(db, user)

    velocity.curves(db, user.id, within_hours=float(settings.velocity_early_window_hours))

    assert len(metrics_read) == 4


def test_the_unbounded_call_still_reads_the_whole_series(
    db, user, long_running_publication
):
    """The bound is opt-in. ``summary`` and the alert pass need the tail, and a
    default that quietly truncated it would break the stall detection that is
    the whole point of storing a series."""
    user = _forget_everything(db, user)

    (curve,) = velocity.curves(db, user.id)

    assert len(curve.points) == _DAYS_LIVE + 2
    assert curve.observed_hours is None


def test_the_calendar_does_not_load_a_year_of_snapshots(
    db, client, auth, user, long_running_publication, metrics_read
):
    """End to end, on the page that regressed.

    ``GET /calendar`` needs a connected platform before it builds any curves at
    all — with none connected the suggestion loop has nothing to iterate and the
    read would be bounded by accident.
    """
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            encrypted_credentials="not-read-in-this-test",
            status=ConnectionStatus.CONNECTED,
        )
    )
    db.commit()
    assert db.query(ContentMetric).count() > 400
    _forget_everything(db, user)
    metrics_read.clear()

    resp = client.get("/api/v1/calendar", headers=auth)
    assert resp.status_code == 200, resp.text

    assert len(metrics_read) <= 10


def test_narrowing_to_a_platform_still_bounds_the_window(
    db, user, long_running_publication, metrics_read
):
    """``observations`` builds its own curves when the caller hands it none.

    That path takes ``platform=`` and is the one the digest and the cadence
    endpoint reach, so the bound has to survive being combined with the filter.
    """
    user = _forget_everything(db, user)
    metrics_read.clear()

    found = learned_cadence.observations(db, user.id, Platform.DEVTO)

    assert len(found) == 1
    assert found[0].early_views == 240
    assert len(metrics_read) == 4


# ---- The refusal --------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        lambda c: c.peak_gain(168.0),
        lambda c: c.recent_gain(168.0),
        lambda c: c.is_stalled(window_hours=168.0, ratio=0.1),
        lambda c: c.views_per_day(),
        lambda c: c.as_dict(),
    ],
)
def test_a_bounded_curve_refuses_the_questions_that_need_the_tail(
    db, user, long_running_publication, question
):
    """Loudly, because the wrong answer here is a believable one.

    A year-old post read through its first day has no readings after that day.
    Left to answer, ``is_stalled`` would find a peak and no recent gain and
    report the post dead — which is true of the truncated series and false of
    the post.
    """
    (bounded,) = velocity.curves(db, user.id, within_hours=24.0)

    with pytest.raises(ValueError, match="within_hours=24"):
        question(bounded)


def test_a_bounded_curve_refuses_a_window_wider_than_it_was_read_for(
    db, user, long_running_publication
):
    """``benchmarks`` asks about two windows; a curve built for the narrower one
    cannot answer the wider, and returning the narrow number would understate
    every benchmark on the dashboard."""
    (bounded,) = velocity.curves(db, user.id, within_hours=24.0)

    assert bounded.views_within(24.0) == 240
    with pytest.raises(ValueError, match="only read through 24h"):
        bounded.views_within(48.0)


def test_an_unbounded_curve_answers_everything(db, user, long_running_publication):
    """The guard must not fire on the curves every other caller builds."""
    (whole,) = velocity.curves(db, user.id)

    assert whole.views_per_day() is not None
    assert whole.peak_gain(168.0) is not None
    assert whole.as_dict()["snapshots"] == _DAYS_LIVE + 2
