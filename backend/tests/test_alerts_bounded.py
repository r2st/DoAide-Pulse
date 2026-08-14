"""The alert pass must not read a year of snapshots to draw ten rows.

``alerts.build`` read every metric snapshot on the account, and the docstring
there argued that it had to. Half of that argument was right and still is: the
trick that bounded ``velocity.summary`` — bracketing the stall verdict between
two aggregates — cannot serve an alert, because an alert's message quotes the
post's ``peak_gain`` and a bracket only ever settles the yes/no.

The other half was wrong. It said computing ``peak_gain`` in SQL needs a
``RANGE`` frame over an *interval*, which PostgreSQL has and SQLite does not, so
a suite running on SQLite put the whole approach out of reach. SQLite has taken
a numeric ``RANGE`` offset since 3.28, and the frame only needs an interval if
the ``ORDER BY`` is a timestamp — order by *seconds* instead and one statement
serves both dialects. That is :func:`velocity.peak_gains`, and with it the pass
needs no series at all: a bounded prefix answers the underperformance half, and
three aggregate queries answer the stalled half.

So there are three claims to defend, and this file makes all three:

* **The arithmetic is the same.** ``peak_gains`` is asserted against the real
  :meth:`Curve.peak_gain` shape by shape, including the shapes that exist to
  break a careless translation: a counter that rewound, a series of nothing but
  NULLs, one that starts NULL and then starts reporting, and readings captured
  before the recorded publish time.

  Two of the guards in :func:`velocity._peak_gain_statement` are **not** pinned
  by anything here, and are kept as insurance rather than as tested behaviour.
  The tie-break on the running clamp's ``ORDER BY`` matters only when two
  readings share a timestamp *and* the engine visits them out of insertion
  order; SQLite scans in rowid order, so removing the tie-break passes this
  suite, and PostgreSQL — where the order genuinely is unspecified — is not here
  to disagree. The millisecond of slack on the ``RANGE`` offset is the same
  story: it covers a float-precision error that the timestamps in these shapes
  do not happen to trigger. Both are cheap and both are right; neither should be
  read as covered.
* **The alerts did not change.** Asserted against the whole pass as it was —
  full curves, :meth:`Curve.is_stalled` — down to the rendered message.
* **The read is bounded.** Counted in ``ContentMetric`` rows reaching the
  session, for the same reason :mod:`tests.test_velocity_summary_budget` counts
  them there: the query count never regressed and would not have caught this.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import alerts, velocity

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
    """A published piece with snapshots at (hours-since-publish, views).

    A negative hour is a snapshot captured *before* the recorded publish time,
    which is the case ``_build_curve`` clamps to hour zero and
    ``peak_gains`` has to clamp identically.
    """
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


def _daily(days: int, views_at) -> list[tuple[float, int | None]]:
    return [(24.0 * day, views_at(day)) for day in range(days)]


# ---- The shapes ---------------------------------------------------------- #
#
# Named for what each is meant to prove about the translation from a Python
# walk over the readings to a window function over the same rows.

SHAPES: dict[str, tuple[float, list[tuple[float, int | None]]]] = {
    # Spiked, then nothing. The ordinary stalled post, and the one the panel
    # exists to surface.
    "spike-then-flat": (
        24 * 60,
        [(0, 0), (6, 400), (24, 900), *_daily(60, lambda d: 900 + d)[2:]],
    ),
    # The best week sits in the middle of the life, where no prefix can see it.
    # A peak read off a prefix would be far too small here.
    "late-peak": (24 * 62, _daily(62, lambda d: 100 + min(max(0, d - 20), 10) * 500)),
    # Still climbing at its last reading. Not stalled.
    "late-and-still-going": (24 * 62, _daily(62, lambda d: 100 + max(0, d - 55) * 500)),
    # Grows at the rate it always has, for a year.
    "steady-for-a-year": (24 * 365, _daily(365, lambda d: 100 * d)),
    "never-landed": (24 * 40, _daily(40, lambda d: 0)),
    # Two windows minus an hour old: no verdict, whatever the numbers say.
    "too-young": (_WINDOW * 2 - 1, [(0, 0), (24, 5000), (100, 5000)]),
    # One reading is not enough to observe a gain: `peak_gain` is None, and the
    # SQL must not report a zero that would read as "observed, and it was none".
    "single-reading": (24 * 40, [(2, 700)]),
    # Every reading landed inside the last window, so there is no baseline.
    "no-baseline": (24 * 60, [(24 * 59, 100), (24 * 59.5, 200)]),
    # The platform reported nothing, every time. NULL is not zero anywhere else
    # here and must not become zero in a window function either.
    "all-null": (24 * 40, _daily(40, lambda d: None)),
    # A counter that went backwards mid-life. The Python clamp keeps the series
    # monotonic; the running MAX in SQL has to agree with it exactly.
    "counter-rewound": (
        24 * 40,
        [(0, 0), (24, 5000), (48, 5200), (72, 40), (96, 60), *_daily(40, lambda d: 70)[5:]],
    ),
    # NULLs interleaved with real readings: the clamp carries the previous
    # total forward rather than dropping to zero.
    "null-gaps": (
        24 * 40,
        [(24.0 * d, None if d % 3 else 100 * d) for d in range(40)],
    ),
    # The platform said nothing for the first two polls and then started
    # reporting. Python's accumulator starts at zero, so those readings are a
    # baseline of zero and the whole of the later total is a gain measured from
    # it. SQL's `MAX` skips NULLs instead, and a series whose first real value
    # is its own baseline shows a gain of almost nothing — which is why the
    # running clamp maximises over `COALESCE(views, 0)` rather than over
    # `views`. Two orders of magnitude apart on this shape, and the only shape
    # here that can tell the two apart.
    "null-then-reporting": (
        24 * 40,
        [(0.0, None), (24.0, None), (48.0, 5000), *_daily(40, lambda d: 5000 + d)[3:]],
    ),
    "still-climbing": (24 * 30, _daily(30, lambda d: 10 * d * d)),
    # A thousand-view jump across a gap wider than the stall window. No window
    # ever held any of it, so the real peak is the size of the trickle after —
    # the shape that catches anything reasoning from the total instead.
    "gap-in-polling": (
        _WINDOW * 12,
        [
            (0.0, 0),
            (_WINDOW * 10, 1000),
            *[
                (_WINDOW * 10 + 24.0 * d, 1000 + d)
                for d in range(1, int(_WINDOW * 2 / 24) + 1)
            ],
        ],
    ),
    # Readings a whole number of days apart, with the stall window itself a
    # whole number of days: every frame boundary lands exactly on a reading,
    # which is where a float comparison is most likely to disagree with
    # Python's. It does not disagree on these timestamps — the slack in
    # `velocity._RANGE_SLACK_SECONDS` is insurance, not something this shape
    # forces — but the boundary case is worth holding still all the same.
    "exactly-on-the-boundary": (
        24 * 60,
        _daily(60, lambda d: 1000 * d),
    ),
    # Backdated publish time: three readings were captured before the post
    # supposedly went live. `_build_curve` puts all three at hour zero, so they
    # are inside every window together however far apart their timestamps are.
    "backdated-publish": (
        24 * 40,
        [(-300.0, 10), (-200.0, 40), (-100.0, 90), *_daily(40, lambda d: 100 + d)],
    ),
    # Two readings captured at the very same instant, the second lower than the
    # first. A regression guard on the value rather than on the tie-break: see
    # the module docstring for why the tie-break itself cannot be pinned here.
    "simultaneous-readings": (
        24 * 40,
        [(0.0, 0), (24.0, 500), (24.0, 300), *_daily(40, lambda d: 600 + d)[2:]],
    ),
}


@pytest.fixture
def every_shape(db, project):
    """One account holding every shape above, keyed by name."""
    return {
        name: _make(db, project, slug=name, published_hours_ago=age, readings=readings)
        for name, (age, readings) in SHAPES.items()
    }


# ---- The arithmetic is the same ------------------------------------------ #


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_database_computes_the_peak_the_python_walk_does(db, user, every_shape, shape):
    """Shape by shape, so a failure names the series that broke it."""
    publication = every_shape[shape]
    (whole,) = velocity.curves(db, user.id, publication_id=publication.id)

    computed = velocity.peak_gains(db, [whole], window_hours=_WINDOW)
    expected = whole.peak_gain(_WINDOW)

    if expected is None:
        # Under two readings. `peak_gains` reports the arithmetic only; the
        # "unknown" is `stall_facts`' job, and the test below pins it.
        assert len(whole.points) < 2
    else:
        assert computed.get(publication.id, 0) == expected, shape


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_aggregated_facts_agree_with_the_full_series(db, user, every_shape, shape):
    """``StallFacts`` has to answer what the curve answers, ``None`` included."""
    publication = every_shape[shape]
    (whole,) = velocity.curves(db, user.id, publication_id=publication.id)
    facts = velocity.stall_facts(db, [whole], window_hours=_WINDOW)[publication.id]

    assert facts.peak_gain == whole.peak_gain(_WINDOW), f"{shape}: peak"
    assert facts.recent_gain == whole.recent_gain(_WINDOW), f"{shape}: recent"
    assert facts.stalled(
        whole.age_hours, window_hours=_WINDOW, ratio=_RATIO
    ) == whole.is_stalled(window_hours=_WINDOW, ratio=_RATIO), f"{shape}: verdict"


def test_the_shapes_reach_both_verdicts(db, user, every_shape):
    """A guard on the fixtures themselves.

    A translation that called nothing stalled would agree with a suite whose
    shapes were all healthy, and bound nothing worth bounding.
    """
    curves = velocity.curves(db, user.id)
    verdicts = {
        c.publication_id: c.is_stalled(window_hours=_WINDOW, ratio=_RATIO)
        for c in curves
    }
    by_name = {name: verdicts[pub.id] for name, pub in every_shape.items()}

    assert by_name["spike-then-flat"] is True
    assert by_name["late-peak"] is True
    assert by_name["still-climbing"] is False
    assert by_name["gap-in-polling"] is False
    assert by_name["too-young"] is False


def test_a_publication_with_no_snapshots_at_all_has_no_facts(db, user, project):
    """Absent, not zero. Nothing was observed, so nothing is known."""
    publication = _make(db, project, slug="unpolled", published_hours_ago=24 * 40, readings=[])
    curves = velocity.curves(db, user.id, publication_id=publication.id)

    assert velocity.stall_facts(db, curves, window_hours=_WINDOW) == {}
    assert velocity.peak_gains(db, curves, window_hours=_WINDOW) == {}
    # And the alert pass reads that absence as "no growth to have stopped".
    assert alerts.build(db, user.id) == []


def test_an_empty_account_asks_the_database_nothing(db, user):
    """No curves, no queries. The guard `_series_facts` already had."""
    assert velocity.stall_facts(db, [], window_hours=_WINDOW) == {}
    assert velocity.peak_gains(db, [], window_hours=_WINDOW) == {}


# ---- The alerts did not change ------------------------------------------- #


def _reference_build(db, user_id, *, limit: int = 10) -> list[alerts.Alert]:
    """``alerts.build`` as it was: every series read whole, every question asked
    of the curve itself.

    Kept deliberately close to the original rather than refactored, because its
    only job is to be the thing the bounded pass is compared against.
    """
    all_curves = velocity.curves(db, user_id)
    grouped: dict[Platform, list[velocity.Curve]] = {}
    for curve in all_curves:
        grouped.setdefault(curve.platform, []).append(curve)

    out: list[alerts.Alert] = []
    for group in grouped.values():
        for curve in group:
            alert = alerts._underperformance(group, curve)
            if alert is None:
                window = float(settings.velocity_stall_window_hours)
                if curve.is_stalled(window_hours=window, ratio=_RATIO):
                    peak = curve.peak_gain(window) or 0
                    recent = curve.recent_gain(window) or 0
                    days = int(window / 24) or 1
                    alert = alerts.Alert(
                        kind="stalled",
                        severity="info",
                        content_id=curve.content_id,
                        publication_id=curve.publication_id,
                        platform=curve.platform,
                        title=curve.title,
                        message=(
                            f"Growth has flattened: {recent:,} views in the last "
                            f"{days} day{'s' if days != 1 else ''} against a best "
                            f"of {peak:,}. A re-share or a refresh would find it a "
                            f"second audience."
                        ),
                        ratio=round(recent / peak, 3) if peak else None,
                        observed=recent,
                        expected=float(peak) if peak else None,
                    )
            if alert is not None:
                out.append(alert)

    out.sort(
        key=lambda a: (
            alerts._SEVERITY_RANK.get(a.severity, 9),
            a.ratio if a.ratio is not None else 1.0,
        )
    )
    return out[:limit]


def test_the_bounded_pass_returns_exactly_the_alerts_the_full_series_did(
    db, user, every_shape
):
    """The whole point: a cheaper answer, not a different one.

    Compared as whole ``Alert`` objects, so the rendered message — the sentence
    that quotes ``peak_gain`` and is the reason a bracket could not serve here —
    has to match too, not only the verdict behind it.
    """
    assert alerts.build(db, user.id) == _reference_build(db, user.id)


def test_the_stalled_alerts_are_not_all_that_came_back(db, user, every_shape):
    """A guard on the assertion above: it must be comparing something."""
    found = alerts.build(db, user.id)

    assert [a for a in found if a.kind == "stalled"], "no stalled alert to compare"


def test_underperformance_alerts_still_come_off_the_prefix(db, user, project):
    """The half that was already answerable from a prefix, still answered.

    Five young posts on one platform, one of them far under the others' median.
    The bounded pass builds these curves with ``within_hours``, so if the
    benchmark window were read off a truncated series this is where it shows.
    """
    for index in range(4):
        _make(
            db,
            project,
            slug=f"healthy-{index}",
            published_hours_ago=72,
            readings=[(0, 0), (24, 800), (48, 2000)],
            platform=Platform.MASTODON,
        )
    weak = _make(
        db,
        project,
        slug="weak",
        published_hours_ago=72,
        readings=[(0, 0), (24, 30), (48, 60)],
        platform=Platform.MASTODON,
    )

    found = alerts.build(db, user.id)

    assert found == _reference_build(db, user.id)
    assert [a.publication_id for a in found if a.kind == "underperforming"] == [weak.id]


def test_the_summary_counts_are_unchanged(db, user, every_shape):
    """``summary`` is what the dashboard badge reads; it wraps ``build``."""
    body = alerts.summary(db, user.id)
    expected = _reference_build(db, user.id)

    assert body["alerts"] == [a.as_dict() for a in expected]
    assert body["warnings"] == sum(1 for a in expected if a.severity == "warning")
    assert body["notices"] == sum(1 for a in expected if a.severity == "info")


# ---- The read is bounded -------------------------------------------------- #


@pytest.fixture
def metrics_read(db):
    """Every ``ContentMetric`` row that reaches the session while this is active.

    Through ``loaded_as_persistent`` rather than the identity map, for the
    reason given at length in :mod:`tests.test_velocity_window_budget`: the rows
    are reduced to ``Point``s and collected before an after-the-fact inspection
    could count them.
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
    """Drop loaded snapshots from the identity map, and only those."""
    for instance in list(db.identity_map.values()):
        if isinstance(instance, ContentMetric):
            db.expunge(instance)


def _year_old_post(db, project, *, slug: str) -> None:
    """One post, up for a year, polled every six hours: 1,461 rows.

    A spike on day one and a trickle ever since — the shape almost every real
    post settles into, and the one that made this pass expensive. Inserted in
    bulk; the fixture has to stay cheap enough to keep.
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
    db.execute(
        ContentMetric.__table__.insert(),
        [
            {
                "publication_id": publication.id,
                "captured_at": published_at + timedelta(hours=6 * step),
                "views": 400 + step // 4,
                "reactions": 1,
            }
            for step in range(4 * 365 + 1)
        ],
    )
    db.commit()


def test_the_rows_read_do_not_grow_with_the_age_of_the_account(
    db, user, project, metrics_read
):
    """Five year-old posts are 7,305 snapshots. The pass reads a handful.

    The bound is per publication: ``velocity_benchmark_window_hours`` of each
    series, which on the default six-hourly cadence is nine readings — and
    nothing at all beyond it, because everything about the tail now arrives as
    an aggregate.
    """
    for index in range(5):
        _year_old_post(db, project, slug=f"year-{index}")
    _forget_the_snapshots(db)
    metrics_read.clear()

    alerts.build(db, user.id)

    benchmark = float(settings.velocity_benchmark_window_hours)
    ceiling = 5 * (int(benchmark / 6) + 2)
    assert len(metrics_read) <= ceiling, (
        f"read {len(metrics_read)} snapshots for 5 posts; "
        f"a bounded pass reads at most {ceiling}"
    )


def test_a_longer_history_is_not_a_bigger_read(db, user, project, metrics_read):
    """The claim stated directly: double the history, same number of rows.

    A ceiling can be met by a pass that is merely small today. What must hold is
    that the read does not track how long the account has been open.
    """
    _year_old_post(db, project, slug="first")
    _forget_the_snapshots(db)
    metrics_read.clear()
    alerts.build(db, user.id)
    one_post = len(metrics_read)

    _year_old_post(db, project, slug="second")
    _forget_the_snapshots(db)
    metrics_read.clear()
    alerts.build(db, user.id)

    assert len(metrics_read) == one_post * 2, "per publication, not per snapshot"


def test_the_bounded_pass_still_agrees_on_a_year_of_history(db, user, project):
    """The two claims together, on the series that motivated the bound."""
    for index in range(4):
        _year_old_post(db, project, slug=f"year-{index}")

    assert alerts.build(db, user.id) == _reference_build(db, user.id)


# ---- One statement, two dialects ------------------------------------------ #


def test_the_frame_compiles_for_both_dialects_the_app_runs_on(db, user, project):
    """PostgreSQL in production, SQLite in the suite, one statement for both.

    The PostgreSQL spelling has no database here to run against, so it is
    checked by compiling — which is enough, because the only thing that differs
    between the two is how ``_EpochSeconds`` renders and the whole risk is that
    one of them renders something else.
    """
    publication = _make(
        db, project, slug="dialects", published_hours_ago=48, readings=[(0, 0), (24, 5)]
    )
    curves = velocity.curves(db, user.id, publication_id=publication.id)
    statement = velocity._peak_gain_statement(
        [c.publication_id for c in curves], window_hours=_WINDOW
    )

    rendered_sqlite = str(statement.compile(dialect=sqlite.dialect()))
    rendered_pg = str(statement.compile(dialect=postgresql.dialect()))

    assert "julianday" in rendered_sqlite
    assert "EXTRACT(EPOCH FROM" in rendered_pg
    # The frame itself is dialect-free: the offset is a number in both, which is
    # the entire reason ordering by seconds was worth doing.
    for rendered in (rendered_sqlite, rendered_pg):
        assert "RANGE BETWEEN" in rendered and "PRECEDING AND CURRENT ROW" in rendered


def test_an_unknown_dialect_refuses_rather_than_guessing(db, user, project):
    """A wrong spelling would not fail, it would under-report the peak.

    Which reads as "this post's best week was small", which reads as "stalled".
    A silent wrong answer on a dashboard is worse than a loud missing one, so
    the fallback raises.
    """
    statement = velocity._peak_gain_statement([1], window_hours=_WINDOW)

    with pytest.raises(NotImplementedError, match="seconds-since-epoch"):
        statement.compile(dialect=mysql.dialect())


def test_the_statement_is_one_query_however_many_publications(db, user, project):
    """One round trip for the whole account, not one per post."""
    for index in range(6):
        _make(
            db,
            project,
            slug=f"many-{index}",
            published_hours_ago=24 * 40,
            readings=_daily(40, lambda d: 100 * d),
        )
    curves = velocity.curves(db, user.id)
    seen: list[str] = []

    def _record(conn, cursor, statement, *args):
        if "velocity_spans" in statement:
            seen.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _record)
    try:
        peaks = velocity.peak_gains(db, curves, window_hours=_WINDOW)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", _record)

    assert len(seen) == 1
    assert len(peaks) == 6


def test_stall_facts_reads_three_queries_for_the_whole_account(db, user, project):
    """Two aggregates for the recent window, one for the peak. That is all."""
    for index in range(6):
        _make(
            db,
            project,
            slug=f"facts-{index}",
            published_hours_ago=24 * 40,
            readings=_daily(40, lambda d: 100 * d),
        )
    curves = velocity.curves(db, user.id)
    seen: list[str] = []

    def _record(conn, cursor, statement, *args):
        if "content_metrics" in statement.lower():
            seen.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _record)
    try:
        facts = velocity.stall_facts(db, curves, window_hours=_WINDOW)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", _record)

    assert len(seen) == 3, seen
    assert len(facts) == 6


def test_the_select_reads_no_snapshot_rows_into_the_session(
    db, user, project, metrics_read
):
    """The aggregates arrive as numbers, not as ORM rows.

    ``peak_gains`` returning entities would bound the *queries* and none of the
    memory, which is the thing this change is for.
    """
    _year_old_post(db, project, slug="aggregate-only")
    curves = velocity.curves(db, user.id, within_hours=1.0)
    _forget_the_snapshots(db)
    metrics_read.clear()

    velocity.stall_facts(db, curves, window_hours=_WINDOW)

    assert metrics_read == []


def test_select_from_the_statement_helper_matches_what_peak_gains_runs(db, user, project):
    """The helper the dialect tests compile is the one the function executes."""
    publication = _make(
        db,
        project,
        slug="same-statement",
        published_hours_ago=24 * 40,
        readings=_daily(40, lambda d: 100 * d),
    )
    curves = velocity.curves(db, user.id, publication_id=publication.id)
    statement = velocity._peak_gain_statement([publication.id], window_hours=_WINDOW)

    direct = {pid: int(gain or 0) for pid, gain in db.execute(statement).all()}

    assert direct == velocity.peak_gains(db, curves, window_hours=_WINDOW)


def test_an_empty_shortlist_selects_no_publication_rather_than_all_of_them(
    db, user, project
):
    """``peak_gains`` short-circuits, but the statement itself must be safe too.

    An ``IN ()`` that matched everything would be a silent cross-account read,
    which is the failure mode worth pinning rather than assuming.
    """
    _make(
        db,
        project,
        slug="present",
        published_hours_ago=24 * 40,
        readings=_daily(40, lambda d: 100 * d),
    )
    statement = velocity._peak_gain_statement([], window_hours=_WINDOW)

    assert db.execute(statement).all() == []
