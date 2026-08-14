"""The velocity panel must not read a year of snapshots to draw ten rows.

``velocity.summary`` built every curve on the account whole. Three of the four
things it returns are questions about the *beginning* of a post's life — the
count, the per-platform medians, the fastest-out-of-the-gate ranking — and the
fourth, ``stalled``, is a question about the end. The fourth dragged the other
three along with it: a post polled every six hours for a year put some 1,400
rows through the dashboard to contribute one number to a median. This was the
last unbounded read left in the service.

The bound cannot come from truncating the series, because ``is_stalled``
genuinely needs the whole thing — that is what ``Curve._require_full_series``
is there to enforce, and
:mod:`tests.test_velocity_window_budget` pins the refusal. It comes instead
from *bracketing* the expensive half. The recent window is two clamped
readings, which the database can hand over exactly; the peak is bounded above
by the whole series' gain and below by any span actually observed, and a recent
window outside that bracket settles the verdict without the tail.

So there are two claims to defend, and this file makes both:

* **The verdicts did not change.** Asserted against the real
  :meth:`Curve.is_stalled` on a full series, shape by shape, including the two
  shapes designed to break a careless bracket: a post whose peak lands in the
  middle of its life where no prefix would see it, and a post that grows
  steadily enough that neither bound settles it.
* **The read is bounded.** Counted in ``ContentMetric`` rows reaching the
  session, for the same reason
  :mod:`tests.test_velocity_window_budget` counts them there: the query count
  never regressed and would not have caught this.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import velocity

_WINDOW = float(settings.velocity_stall_window_hours)
_RATIO = settings.velocity_stall_ratio


def _make(
    db,
    project,
    *,
    slug: str,
    published_hours_ago: float,
    readings: Sequence[tuple[float, int | None]],
    platform: Platform = Platform.DEVTO,
) -> Publication:
    """A published piece with snapshots at (hours-since-publish, views)."""
    published_at = utcnow() - timedelta(hours=published_hours_ago)
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Post {slug}",
        slug=slug,
        status=ContentStatus.PUBLISHED,
        published_at=published_at,
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_id=f"ext-{slug}",
    )
    db.add(publication)
    db.flush()
    for hours, views in readings:
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=published_at + timedelta(hours=hours),
                views=views,
                reactions=1,
            )
        )
    db.commit()
    db.refresh(publication)
    return publication


# ---- The shapes ---------------------------------------------------------- #
#
# One entry per way a series can be shaped, named for what it is meant to prove
# about the bracket rather than for what it looks like. Each is fed to the
# bounded path and to the full-series path and the two have to agree.


def _daily(days: int, views_at) -> list[tuple[float, int | None]]:
    return [(24.0 * day, views_at(day)) for day in range(days)]


SHAPES: dict[str, tuple[float, list[tuple[float, int | None]]]] = {
    # Spiked, then nothing. The ordinary stalled post, and the one the panel
    # exists to surface.
    "spike-then-flat": (
        24 * 60,
        [(0, 0), (6, 400), (24, 900), *_daily(60, lambda d: 900 + d)[2:]],
    ),
    # Flat, then a burst three weeks in, then flat again. The best week sits in
    # the middle of the life, where no prefix of the series can see it: a lower
    # bound that only ever looked at the prefix would find a peak of nothing and
    # be unable to settle this, and one that mistook the prefix for the peak
    # would settle it for the wrong reason.
    "late-peak": (
        24 * 62,
        _daily(62, lambda d: 100 + min(max(0, d - 20), 10) * 500),
    ),
    # Still climbing at the moment of its last reading, having been flat for
    # weeks before that. Not stalled — and the shape that catches a bracket
    # measuring the recent window from *now* rather than from the last poll.
    "late-and-still-going": (
        24 * 62,
        _daily(62, lambda d: 100 + max(0, d - 55) * 500),
    ),
    # Grows at the same rate it always has. Not stalled, and past a year the
    # bracket cannot prove it either way — this is the shape that must fall
    # through to the full series rather than be guessed at.
    "steady-for-a-year": (24 * 365, _daily(365, lambda d: 100 * d)),
    # Nothing ever happened. A different problem, and not this one.
    "never-landed": (24 * 40, _daily(40, lambda d: 0)),
    # Two windows minus an hour old: no verdict, whatever the numbers say.
    "too-young": (_WINDOW * 2 - 1, [(0, 0), (24, 5000), (100, 5000)]),
    # Polled once and never again. `peak_gain` is None on one reading.
    "single-reading": (24 * 40, [(2, 700)]),
    # Live for months, but every reading landed inside the last window: there
    # is no baseline to subtract, so the recent gain is unknown, not zero.
    "no-baseline": (24 * 60, [(24 * 59, 100), (24 * 59.5, 200)]),
    # The platform reported nothing, every time. NULL is not zero anywhere else
    # here and must not become zero in an aggregate either.
    "all-null": (24 * 40, _daily(40, lambda d: None)),
    # A counter that went backwards mid-life. The clamp keeps the series
    # monotonic; `MAX(views)` has to agree with it.
    "counter-rewound": (
        24 * 40,
        [(0, 0), (24, 5000), (48, 5200), (72, 40), (96, 60), *_daily(40, lambda d: 70)[5:]],
    ),
    # Still climbing hard in its most recent window.
    "still-climbing": (24 * 30, _daily(30, lambda d: 10 * d * d)),
}


@pytest.fixture
def every_shape(db, project):
    """One account holding every shape above, keyed by name."""
    return {
        name: _make(db, project, slug=name, published_hours_ago=age, readings=readings)
        for name, (age, readings) in SHAPES.items()
    }


def _reference_stalled(db, user_id) -> list[int]:
    """What ``summary`` returned before it was bounded: read everything, ask."""
    return [
        curve.publication_id
        for curve in velocity.curves(db, user_id)
        if curve.is_stalled(window_hours=_WINDOW, ratio=_RATIO)
    ][:10]


# ---- The verdicts did not change ----------------------------------------- #


def test_the_bounded_panel_lists_exactly_the_posts_the_full_series_calls_stalled(
    db, user, every_shape
):
    """The whole point: a cheaper answer, not a different one."""
    body = velocity.summary(db, user.id)

    assert [row["publication_id"] for row in body["stalled"]] == _reference_stalled(
        db, user.id
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_bracket_never_disagrees_with_the_full_series(db, user, every_shape, shape):
    """Shape by shape, so a failure names the series that broke it.

    ``_stall_verdict`` is allowed to answer ``None`` — that is it declining to
    guess, and ``summary`` reads the tail for those. What it is never allowed to
    do is answer, and be wrong.
    """
    publication = every_shape[shape]
    (prefix,) = velocity.curves(
        db,
        user.id,
        publication_id=publication.id,
        within_hours=max(
            float(settings.velocity_early_window_hours),
            float(settings.velocity_benchmark_window_hours),
        ),
    )
    (whole,) = velocity.curves(db, user.id, publication_id=publication.id)
    facts = velocity._series_facts(db, [prefix], window_hours=_WINDOW)

    guessed = velocity._stall_verdict(
        prefix, facts.get(publication.id), window_hours=_WINDOW, ratio=_RATIO
    )
    truth = whole.is_stalled(window_hours=_WINDOW, ratio=_RATIO)

    assert guessed in (None, truth), f"{shape}: said {guessed}, series says {truth}"


def test_the_shapes_exercise_both_sides_of_the_bracket_and_the_gap(
    db, user, every_shape
):
    """A guard on the fixtures themselves.

    Every branch of ``_stall_verdict`` has to be reached by something here, or
    the parametrised test above passes by never testing anything: a bracket
    that answered ``None`` to everything would agree with every series and bound
    nothing at all.
    """
    prefix_window = max(
        float(settings.velocity_early_window_hours),
        float(settings.velocity_benchmark_window_hours),
    )
    prefixes = velocity.curves(db, user.id, within_hours=prefix_window)
    facts = velocity._series_facts(db, prefixes, window_hours=_WINDOW)
    verdicts = {
        curve.publication_id: velocity._stall_verdict(
            curve, facts.get(curve.publication_id), window_hours=_WINDOW, ratio=_RATIO
        )
        for curve in prefixes
    }
    by_name = {name: verdicts[pub.id] for name, pub in every_shape.items()}

    assert by_name["spike-then-flat"] is True, "settled stalled without the tail"
    assert by_name["late-peak"] is True, "a peak no prefix saw, settled by pigeonhole"
    assert by_name["still-climbing"] is False, "settled not-stalled by the upper bound"
    assert by_name["too-young"] is False, "settled by age alone, on no rows at all"
    assert by_name["steady-for-a-year"] is None, "declined to guess, as it must"


def test_the_first_window_numbers_are_untouched(db, user, every_shape):
    """``fastest`` and ``benchmarks`` come off the prefix now. Same numbers."""
    body = velocity.summary(db, user.id)
    whole = velocity.curves(db, user.id)
    early = float(settings.velocity_early_window_hours)

    expected = sorted(
        (c for c in whole if c.views_within(early) is not None),
        key=lambda c: c.views_within(early) or 0,
        reverse=True,
    )[:5]

    assert body["publications"] == len(whole)
    assert [row["publication_id"] for row in body["fastest"]] == [
        c.publication_id for c in expected
    ]
    assert body["benchmarks"] == [
        b.as_dict() for b in velocity.benchmarks(db, user.id, known=whole)
    ]


def test_the_rendered_rows_still_carry_their_lifetime_numbers(db, user, every_shape):
    """``as_dict`` is a full-series answer and has to stay one.

    ``snapshots`` and ``views_per_day`` are counted over a whole life. Served
    off a prefix they would be quietly, plausibly small — the failure this
    module's guard exists to prevent, arriving through the front door.
    """
    body = velocity.summary(db, user.id)
    whole = {c.publication_id: c for c in velocity.curves(db, user.id)}

    assert body["fastest"], "nothing rendered, so nothing asserted"
    for row in body["fastest"] + body["stalled"]:
        expected = whole[row["publication_id"]]
        assert row["snapshots"] == len(expected.points)
        assert row["views_per_day"] == expected.views_per_day()
        assert row["stalled"] == expected.is_stalled(
            window_hours=_WINDOW, ratio=_RATIO
        )


# ---- The read is bounded -------------------------------------------------- #


@pytest.fixture
def metrics_read(db):
    """Every ``ContentMetric`` row that reaches the session while this is active.

    Through ``loaded_as_persistent`` rather than the identity map, for the
    reason given at length in :mod:`tests.test_velocity_window_budget`: the
    rows are reduced to ``Point``s and collected before an after-the-fact
    inspection could count them.
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


def _forget_the_snapshots(db) -> None:
    """Drop loaded snapshots from the identity map, and only those.

    A row already in the identity map is handed back without a fresh load, so
    without this the rows the fixture inserted would go uncounted.
    :mod:`tests.test_velocity_window_budget` reaches for ``expunge_all`` and
    then re-fetches the user; that detaches the fixtures too, and the tests
    below go on inserting posts into ``project`` after they have counted
    something. Only the snapshots are in the way, so only the snapshots go.
    """
    for instance in list(db.identity_map.values()):
        if isinstance(instance, ContentMetric):
            db.expunge(instance)


_POLLS_A_YEAR = 4 * 365


def _year_old_post(db, project, *, slug: str, first_day_views: int) -> int:
    """One post, up for a year, polled every six hours, stalled for most of it.

    A spike on day one and a trickle ever since — the shape almost every real
    post settles into, and the one that made ``summary`` expensive: 1,461 rows
    to say "this stopped growing".

    The snapshots go in through a bulk insert rather than the ORM. Twenty of
    these is 29,000 rows and the fixture has to stay cheap enough to keep.
    """
    published_at = utcnow() - timedelta(hours=24 * 365)
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Post {slug}",
        slug=slug,
        status=ContentStatus.PUBLISHED,
        published_at=published_at,
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_id=f"ext-{slug}",
    )
    db.add(publication)
    db.flush()

    day_one = [0, first_day_views // 2, first_day_views * 3 // 4, first_day_views]
    db.execute(
        ContentMetric.__table__.insert(),
        [
            {
                "publication_id": publication.id,
                "captured_at": published_at + timedelta(hours=6 * step),
                # Day one, then one more view a day for the rest of the year.
                "views": day_one[step] if step < 4 else first_day_views + step // 4,
                "reactions": 1,
            }
            for step in range(_POLLS_A_YEAR)
        ],
    )
    db.commit()
    return publication.id


@pytest.fixture
def a_year_of_polling(db, project) -> list[int]:
    """Twenty of those: twenty-nine thousand snapshots, fifteen rendered rows."""
    return [
        _year_old_post(db, project, slug=f"year-{index}", first_day_views=900 + index)
        for index in range(20)
    ]


def test_the_panel_does_not_read_the_whole_history_of_the_account(
    db, user, a_year_of_polling, metrics_read
):
    """The bound: the prefix of each publication, and nothing else at all.

    Nine readings apiece — the six-hourly polls through the wider of the two
    benchmark windows, inclusive of both ends. Not "fewer than before": the
    rendered rows are served from aggregates too, so no series is read whole
    here, and a regression that reintroduced one would show up as 1,461 rows
    rather than as a slightly worse constant.
    """
    widest = settings.velocity_benchmark_window_hours
    prefix = len([step for step in range(_POLLS_A_YEAR) if 6 * step <= widest])
    total = db.query(ContentMetric).count()
    assert total > 29_000, "the fixture is the point; keep it big"
    _forget_the_snapshots(db)

    velocity.summary(db, user.id)

    assert len(metrics_read) == prefix * len(a_year_of_polling) == 180


def test_the_panel_agrees_with_itself_on_a_year_of_polling(
    db, user, a_year_of_polling
):
    """The bound, on the fixture that made it necessary."""
    body = velocity.summary(db, user.id)
    whole = {c.publication_id: c for c in velocity.curves(db, user.id)}

    assert body["publications"] == len(a_year_of_polling)
    assert [row["publication_id"] for row in body["stalled"]] == _reference_stalled(
        db, user.id
    )
    for row in body["stalled"]:
        expected = whole[row["publication_id"]]
        assert row["snapshots"] == len(expected.points) == _POLLS_A_YEAR
        assert row["views"] == expected.points[-1].views
        assert row["views_per_day"] == expected.views_per_day()


def test_adding_a_year_of_history_does_not_add_a_year_of_reads(
    db, project, user, metrics_read
):
    """The regression in one assertion: cost tracks the account, not its age.

    Two posts side by side, one polled for a fortnight and one for a year. Both
    are rendered. The older one must not cost twenty-six times what the younger
    one does.
    """

    def _cost() -> int:
        _forget_the_snapshots(db)
        metrics_read.clear()
        velocity.summary(db, user.id)
        return len(metrics_read)

    _make(
        db,
        project,
        slug="a-fortnight",
        published_hours_ago=24 * 14,
        readings=[(float(6 * step), 500 + step) for step in range(4 * 14)],
    )
    a_fortnight = _cost()

    _year_old_post(db, project, slug="a-year", first_day_views=900)
    both = _cost()

    assert both == a_fortnight * 2, "each publication costs its prefix, once"


# ---- The pieces the bound is built from ----------------------------------- #


def test_observed_peak_gain_is_the_peak_when_the_series_is_whole(db, project, user):
    """The lower bound is only useful if it is tight where it can be."""
    _make(
        db,
        project,
        slug="a",
        published_hours_ago=24 * 40,
        readings=[(0, 0), (24, 100), (200, 900), (400, 950)],
    )
    (whole,) = velocity.curves(db, user.id)

    assert whole.observed_peak_gain(_WINDOW) == whole.peak_gain(_WINDOW)


def test_observed_peak_gain_never_overstates_what_a_truncated_curve_saw(
    db, project, user
):
    """A prefix may under-report the peak. It may never over-report it."""
    _make(
        db,
        project,
        slug="a",
        published_hours_ago=24 * 40,
        readings=[(0, 0), (24, 100), (200, 9000), (400, 9100)],
    )
    (whole,) = velocity.curves(db, user.id)
    (prefix,) = velocity.curves(db, user.id, within_hours=48.0)

    assert prefix.observed_peak_gain(_WINDOW) == 100
    assert prefix.observed_peak_gain(_WINDOW) <= whole.peak_gain(_WINDOW)


def test_a_truncated_curve_still_refuses_the_question_it_cannot_answer(
    db, project, user
):
    """``observed_peak_gain`` is a deliberate exception, not a hole.

    It answers on a prefix because it promises a bound rather than a value.
    ``peak_gain`` promises the value and still refuses.
    """
    _make(db, project, slug="a", published_hours_ago=24 * 40, readings=[(0, 0), (24, 9)])
    (prefix,) = velocity.curves(db, user.id, within_hours=48.0)

    assert prefix.observed_peak_gain(_WINDOW) == 9
    with pytest.raises(ValueError, match="peak_gain needs the whole series"):
        prefix.peak_gain(_WINDOW)


def test_asking_for_no_publications_returns_no_curves(db, project, user):
    """An empty shortlist means an empty shortlist.

    ``summary`` hands ``publication_ids`` whatever the bracket left unsettled,
    which is usually nothing. A filter that treated the empty list as "no
    filter" would turn the cheapest case into the most expensive one.
    """
    _make(db, project, slug="a", published_hours_ago=100, readings=[(24, 10)])

    assert velocity.curves(db, user.id, publication_ids=[]) == []
    assert len(velocity.curves(db, user.id, publication_ids=None)) == 1


def test_narrowing_to_several_publications_reads_only_those(
    db, project, user, metrics_read
):
    """The multi-id filter belongs in the query, like the single-id one."""
    kept = [
        _make(
            db,
            project,
            slug=f"kept-{index}",
            published_hours_ago=100,
            readings=[(6, 10), (24, 20)],
        ).id
        for index in range(2)
    ]
    _make(
        db,
        project,
        slug="ignored",
        published_hours_ago=100,
        readings=[(float(n), n) for n in range(50)],
    )
    _forget_the_snapshots(db)
    metrics_read.clear()

    found = velocity.curves(db, user.id, publication_ids=kept)

    assert sorted(c.publication_id for c in found) == sorted(kept)
    assert len(metrics_read) == 4


def test_series_facts_match_the_series_they_summarise(db, project, user):
    """The aggregates stand in for a read of the whole series. They must agree.

    Including on the two shapes where a naive aggregate diverges from the
    clamped curve: a counter that went backwards, and a NULL that is not a zero.
    """
    _make(
        db,
        project,
        slug="rewound",
        published_hours_ago=24 * 60,
        readings=[(0, 0), (24, 5000), (48, 40), (24 * 50, None), (24 * 59, 5100)],
    )
    (whole,) = velocity.curves(db, user.id)
    (prefix,) = velocity.curves(db, user.id, within_hours=48.0)

    (facts,) = velocity._series_facts(db, [prefix], window_hours=_WINDOW).values()

    assert facts.snapshots == len(whole.points)
    assert facts.total_views == whole.points[-1].views == 5100
    # The recent gain the aggregates imply is the one the series computes.
    assert facts.total_views - facts.baseline_views == whole.recent_gain(_WINDOW)
