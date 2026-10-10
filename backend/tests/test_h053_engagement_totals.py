"""Adding engagement up across platforms respects what each one reports.

The dashboard numbers come from :mod:`app.services.content_engagement`, which
sums per-platform readings into per-piece totals. The difficulty is not the
arithmetic — it is the provenance: Dev.to reports views, Bluesky does not, and
a total that silently zeros the absent number turns "Bluesky does not report
views" into "this post got no views on Bluesky". Every assertion here is about
that distinction surviving the summation.
"""
from __future__ import annotations

from app.models.publication import Platform
from app.services.content_engagement import (
    FIELDS,
    PlatformEngagement,
    _totals,
)


def _platform(
    platform: Platform,
    *,
    views: int | None = None,
    reactions: int | None = None,
    comments: int | None = None,
    clicks: int | None = None,
    shares: int | None = None,
    reads: int | None = None,
) -> PlatformEngagement:
    return PlatformEngagement(
        publication_id=1,
        platform=platform,
        external_url=None,
        published_at=None,
        views=views,
        reactions=reactions,
        comments=comments,
        clicks=clicks,
        shares=shares,
        reads=reads,
    )


def test_a_field_no_platform_reports_stays_none():
    totals = _totals([
        _platform(Platform.BLUESKY, reactions=5),
        _platform(Platform.MASTODON, reactions=3),
    ])
    assert totals.views is None
    assert "views" not in totals.reported_by


def test_a_field_one_platform_reports_is_that_platforms_number():
    totals = _totals([
        _platform(Platform.DEVTO, views=100, reactions=10),
        _platform(Platform.BLUESKY, reactions=5),
    ])
    assert totals.views == 100
    assert totals.reported_by["views"] == ["devto"]


def test_two_platforms_reporting_the_same_field_are_summed():
    totals = _totals([
        _platform(Platform.DEVTO, views=100, reactions=10),
        _platform(Platform.MEDIUM, views=50, reactions=3),
    ])
    assert totals.views == 150
    assert sorted(totals.reported_by["views"]) == ["devto", "medium"]


def test_engagement_adds_the_four_interaction_counters():
    pe = _platform(Platform.DEVTO, reactions=10, comments=3, clicks=2, shares=1)
    assert pe.engagement == 16


def test_engagement_with_all_none_is_zero():
    pe = _platform(Platform.BLUESKY)
    assert pe.engagement == 0


def test_totals_engagement_matches_platform_sum():
    platforms = [
        _platform(Platform.DEVTO, reactions=10, comments=3),
        _platform(Platform.BLUESKY, reactions=5, shares=2),
    ]
    totals = _totals(platforms)
    assert totals.engagement == sum(p.engagement for p in platforms)


def test_empty_platforms_produce_empty_totals():
    totals = _totals([])
    for name in FIELDS:
        assert getattr(totals, name) is None
    assert totals.reported_by == {}
    assert totals.engagement == 0


def test_as_dict_preserves_none_fields():
    pe = _platform(Platform.BLUESKY, reactions=5)
    d = pe.as_dict()
    assert d["views"] is None
    assert d["reactions"] == 5
    assert d["engagement"] == 5
    assert d["platform"] == "bluesky"


def test_totals_as_dict_includes_reported_by():
    totals = _totals([_platform(Platform.DEVTO, views=42)])
    d = totals.as_dict()
    assert d["views"] == 42
    assert "views" in d["reported_by"]
    assert d["reads"] is None
