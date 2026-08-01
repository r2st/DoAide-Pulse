"""Reading the snapshot series: windows, gains, and what "unknown" means.

Three things this file exists to pin down:

* Platform counters are **cumulative**, so a window's number is a *reading*,
  and a gain is a *subtraction*. Summing snapshots would count the same views
  once per poll.
* An unobserved window is ``None``, never zero. A post that is twelve hours old
  has no first-day number, and neither does one nothing polled in time.
* A counter that goes backwards does not produce negative growth.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import velocity


def _now() -> datetime:
    return datetime.now(UTC)


def make_post(
    db,
    project,
    *,
    slug: str,
    published_hours_ago: float,
    readings: Sequence[tuple[float, int]],
    platform: Platform = Platform.DEVTO,
    engagement_per_reading: int = 0,
) -> Publication:
    """A published piece with snapshots at (hours-since-publish, views)."""
    published_at = _now() - timedelta(hours=published_hours_ago)
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
                reactions=engagement_per_reading,
            )
        )
    db.commit()
    db.refresh(publication)
    return publication


# --------------------------------------------------------------------------- #
# Curve construction                                                          #
# --------------------------------------------------------------------------- #


def test_curve_positions_readings_against_publication(db, project, user):
    make_post(
        db, project, slug="a", published_hours_ago=100, readings=[(6, 10), (24, 90)]
    )
    (curve,) = velocity.curves(db, user.id)

    assert [round(p.hours) for p in curve.points] == [6, 24]
    assert [p.views for p in curve.points] == [10, 90]
    assert curve.age_hours == pytest.approx(100, abs=0.1)


def test_counter_going_backwards_does_not_shrink_the_curve(db, project, user):
    """A purge or a rescrape is not negative growth."""
    make_post(
        db, project, slug="a", published_hours_ago=100, readings=[(6, 500), (24, 300)]
    )
    (curve,) = velocity.curves(db, user.id)

    assert [p.views for p in curve.points] == [500, 500]
    # Between the two readings that exist: flat, not -200.
    assert curve.gain_between(6, 24) == 0


def test_missing_views_carry_the_previous_total_forward(db, project, user):
    """A NULL means the platform said nothing, not that views went to zero."""
    publication = make_post(
        db, project, slug="a", published_hours_ago=100, readings=[(6, 200)]
    )
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=publication.published_at + timedelta(hours=24),
            views=None,
            reactions=3,
        )
    )
    db.commit()

    (curve,) = velocity.curves(db, user.id)
    assert [p.views for p in curve.points] == [200, 200]


def test_unpublished_publications_are_skipped(db, project, user):
    """Every window is measured from publication; without one there is no curve."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Queued",
        slug="queued",
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.flush()
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
        )
    )
    db.commit()

    assert velocity.curves(db, user.id) == []


def test_curves_are_scoped_to_the_user(db, project, user):
    """Another user's project must not appear in these numbers."""
    make_post(db, project, slug="mine", published_hours_ago=100, readings=[(24, 50)])
    assert len(velocity.curves(db, user.id)) == 1
    assert velocity.curves(db, user.id + 999) == []


# --------------------------------------------------------------------------- #
# Windows, and the discipline about unknowns                                  #
# --------------------------------------------------------------------------- #


def test_first_day_reads_the_covering_snapshot(db, project, user):
    make_post(
        db,
        project,
        slug="a",
        published_hours_ago=100,
        readings=[(6, 40), (20, 300), (48, 900)],
    )
    (curve,) = velocity.curves(db, user.id)

    # The 20h reading covers the 24h window; the 48h one is outside it.
    assert curve.views_within(24) == 300
    assert curve.views_within(48) == 900


def test_a_post_younger_than_the_window_has_no_number_for_it(db, project, user):
    make_post(db, project, slug="a", published_hours_ago=10, readings=[(6, 40)])
    (curve,) = velocity.curves(db, user.id)

    assert curve.views_within(6) == 40
    assert curve.views_within(24) is None, "not yet a day old"


def test_a_window_nothing_covered_is_unknown_not_zero(db, project, user):
    """One poll at +2h cannot stand for the first day."""
    make_post(
        db, project, slug="a", published_hours_ago=100, readings=[(2, 5), (40, 800)]
    )
    (curve,) = velocity.curves(db, user.id)

    assert curve.views_within(24) is None
    assert curve.views_within(48) == 800


def test_a_post_with_no_snapshots_reports_unknown(db, project, user):
    make_post(db, project, slug="a", published_hours_ago=100, readings=[])
    (curve,) = velocity.curves(db, user.id)

    assert curve.views_within(24) is None
    assert curve.views_per_day() is None
    assert curve.peak_gain(24) is None


# --------------------------------------------------------------------------- #
# Gains and decay                                                             #
# --------------------------------------------------------------------------- #


def test_gain_is_a_subtraction_not_a_sum(db, project, user):
    make_post(
        db,
        project,
        slug="a",
        published_hours_ago=200,
        readings=[(24, 100), (48, 150), (72, 175)],
    )
    (curve,) = velocity.curves(db, user.id)

    # Summing the readings would give 425. The post gained 75.
    assert curve.gain_between(24, 72) == 75


def test_peak_gain_finds_the_best_window_anywhere_in_the_series(db, project, user):
    make_post(
        db,
        project,
        slug="a",
        published_hours_ago=500,
        # Quiet, then an aggregator picks it up, then quiet again.
        readings=[(0, 0), (24, 10), (48, 20), (72, 1020), (96, 1030), (120, 1035)],
    )
    (curve,) = velocity.curves(db, user.id)

    assert curve.peak_gain(24) == 1000


def test_a_post_that_stopped_growing_is_stalled(db, project, user):
    readings = [(0, 0), (24, 900), (48, 1000)]
    readings += [(48 + 24 * n, 1000 + n) for n in range(1, 16)]
    make_post(db, project, slug="a", published_hours_ago=600, readings=readings)
    (curve,) = velocity.curves(db, user.id)

    assert curve.is_stalled(window_hours=168, ratio=0.1) is True


def test_a_post_still_growing_is_not_stalled(db, project, user):
    readings = [(24 * n, 500 * n) for n in range(0, 21)]
    make_post(db, project, slug="a", published_hours_ago=600, readings=readings)
    (curve,) = velocity.curves(db, user.id)

    assert curve.is_stalled(window_hours=168, ratio=0.1) is False


def test_a_young_post_is_never_stalled(db, project, user):
    """Two full windows of history, or no verdict."""
    make_post(
        db, project, slug="a", published_hours_ago=48, readings=[(0, 0), (24, 900)]
    )
    (curve,) = velocity.curves(db, user.id)

    assert curve.is_stalled(window_hours=168, ratio=0.1) is False


def test_a_post_that_never_landed_is_not_called_stalled(db, project, user):
    """Zero growth throughout is a different problem, and not this one."""
    make_post(
        db,
        project,
        slug="a",
        published_hours_ago=600,
        readings=[(24 * n, 0) for n in range(0, 21)],
    )
    (curve,) = velocity.curves(db, user.id)

    assert curve.is_stalled(window_hours=168, ratio=0.1) is False


# --------------------------------------------------------------------------- #
# Benchmarks                                                                  #
# --------------------------------------------------------------------------- #


def test_benchmark_is_a_median_per_platform(db, project, user):
    for index, views in enumerate([100, 200, 900]):
        make_post(
            db,
            project,
            slug=f"devto-{index}",
            published_hours_ago=200,
            readings=[(24, views // 2), (46, views)],
        )
    make_post(
        db,
        project,
        slug="bluesky-0",
        published_hours_ago=200,
        readings=[(24, 10), (46, 20)],
        platform=Platform.BLUESKY,
    )

    found = {b.platform: b for b in velocity.benchmarks(db, user.id)}
    # Median, not mean: the 900 outlier must not set the bar at 400.
    assert found[Platform.DEVTO].median_benchmark_views == 200
    assert found[Platform.DEVTO].reliable is True
    assert found[Platform.BLUESKY].reliable is False, "one post is not a benchmark"


def test_benchmark_excludes_the_post_being_judged(db, project, user):
    """Leave-one-out: a post must not drag down the bar it is measured against."""
    for index, views in enumerate([1000, 1000, 1000, 4]):
        make_post(
            db,
            project,
            slug=f"p-{index}",
            published_hours_ago=200,
            readings=[(46, views)],
        )
    curves = velocity.curves(db, user.id)
    subject = min(curves, key=lambda c: c.views_within(48) or 0)

    including = velocity.benchmarks(db, user.id)[0].median_benchmark_views
    excluding = velocity.benchmark_excluding(curves, subject, hours=48)

    assert including == 1000  # median of [4, 1000, 1000, 1000]
    assert excluding == 1000  # median of [1000, 1000, 1000]


def test_benchmark_excluding_needs_a_minimum_sample(db, project, user):
    make_post(db, project, slug="a", published_hours_ago=200, readings=[(46, 100)])
    make_post(db, project, slug="b", published_hours_ago=200, readings=[(46, 100)])
    curves = velocity.curves(db, user.id)

    # Two posts means one other post to compare against — not a verdict.
    assert velocity.benchmark_excluding(curves, curves[0], hours=48) is None


def test_summary_is_empty_and_serializable_with_no_data(db, user):
    body = velocity.summary(db, user.id)
    assert body["publications"] == 0
    assert body["benchmarks"] == []
    assert body["fastest"] == []
    assert body["stalled"] == []


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def test_velocity_endpoint_reports_the_windows(client, auth, db, project):
    make_post(
        db, project, slug="a", published_hours_ago=200, readings=[(20, 300), (46, 800)]
    )
    resp = client.get("/api/v1/analytics/velocity", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["publications"] == 1
    assert body["early_window_hours"] == settings.velocity_early_window_hours
    assert body["fastest"][0]["views_first_24h"] == 300


def test_velocity_curve_endpoint_returns_the_points(client, auth, db, project):
    publication = make_post(
        db, project, slug="a", published_hours_ago=200, readings=[(20, 300), (46, 800)]
    )
    resp = client.get(f"/api/v1/analytics/velocity/{publication.id}", headers=auth)

    assert resp.status_code == 200
    assert [p["views"] for p in resp.json()["points"]] == [300, 800]


def test_velocity_curve_endpoint_404s_for_an_unknown_publication(client, auth):
    assert client.get("/api/v1/analytics/velocity/424242", headers=auth).status_code == 404


def test_velocity_endpoints_require_auth(client):
    assert client.get("/api/v1/analytics/velocity").status_code == 401
    assert client.get("/api/v1/analytics/alerts").status_code == 401
