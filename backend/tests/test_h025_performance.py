"""H025 performance round — test cases for the two findings.

1. ``content_engagement._trend`` used an O(S·C·P) scan (re-filtering every
   curve's points from the start on each timestamp).  The fix advances a
   per-curve pointer so each point is visited once, giving O(S·C + total_P).

2. ``digest._last_readings_before`` grouped on ``MAX(captured_at)``, which
   can tie (two polls in the same second), then joined back on the tied
   column — potentially returning duplicate rows.  The fix uses ``MAX(id)``
   instead, which is monotonic and unique.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services.velocity import Curve, Point


# ── Finding 1: _trend pointer-advance produces the same output ────────────


def _make_curve(
    publication_id: int,
    points: list[tuple[float, int, int]],
) -> Curve:
    return Curve(
        publication_id=publication_id,
        content_id=publication_id * 10,
        platform="devto",
        title=f"Post {publication_id}",
        published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        age_hours=999.0,
        points=[Point(hours=h, views=v, engagement=e) for h, v, e in points],
    )


def _trend_naive(curves: list[Curve]) -> list[dict]:
    """The original O(S·C·P) implementation, for comparison."""
    if not curves:
        return []
    stamps = sorted({round(p.hours, 2) for curve in curves for p in curve.points})
    latest: dict[int, tuple[int, int]] = {}
    out: list[dict] = []
    for hours in stamps:
        for curve in curves:
            due = [p for p in curve.points if round(p.hours, 2) <= hours]
            if due:
                latest[curve.publication_id] = (due[-1].views, due[-1].engagement)
        out.append(
            {
                "hours": hours,
                "views": sum(v for v, _ in latest.values()),
                "engagement": sum(e for _, e in latest.values()),
            }
        )
    return out


class TestTrendPointerAdvance:
    """The optimised _trend must match the naive implementation exactly."""

    def test_empty(self):
        from app.services.content_engagement import _trend

        assert _trend([]) == []

    def test_single_curve(self):
        from app.services.content_engagement import _trend

        curves = [_make_curve(1, [(1.0, 10, 2), (6.0, 50, 8), (12.0, 100, 15)])]
        assert _trend(curves) == _trend_naive(curves)

    def test_two_curves_interleaved(self):
        from app.services.content_engagement import _trend

        curves = [
            _make_curve(1, [(1.0, 10, 1), (3.0, 30, 3), (5.0, 50, 5)]),
            _make_curve(2, [(2.0, 20, 2), (4.0, 40, 4), (6.0, 60, 6)]),
        ]
        result = _trend(curves)
        expected = _trend_naive(curves)
        assert result == expected
        assert len(result) == 6

    def test_shared_timestamps(self):
        """Two curves with readings at the same hour."""
        from app.services.content_engagement import _trend

        curves = [
            _make_curve(1, [(1.0, 10, 1), (2.0, 20, 2)]),
            _make_curve(2, [(1.0, 5, 0), (2.0, 15, 1)]),
        ]
        result = _trend(curves)
        expected = _trend_naive(curves)
        assert result == expected
        assert result[0] == {"hours": 1.0, "views": 15, "engagement": 1}
        assert result[1] == {"hours": 2.0, "views": 35, "engagement": 3}

    def test_one_curve_ahead_of_other(self):
        """Curve 2 starts after all of curve 1's readings — last-known carry-over."""
        from app.services.content_engagement import _trend

        curves = [
            _make_curve(1, [(1.0, 100, 10)]),
            _make_curve(2, [(5.0, 50, 5), (10.0, 200, 20)]),
        ]
        result = _trend(curves)
        expected = _trend_naive(curves)
        assert result == expected
        assert result[0] == {"hours": 1.0, "views": 100, "engagement": 10}
        assert result[1] == {"hours": 5.0, "views": 150, "engagement": 15}
        assert result[2] == {"hours": 10.0, "views": 300, "engagement": 30}

    def test_many_points_matches_naive(self):
        """Stress: 200 points per curve, three curves — the fix must still agree."""
        from app.services.content_engagement import _trend

        curves = [
            _make_curve(
                i,
                [
                    (round(h * 0.5 + i * 0.1, 2), h * 10 + i, h + i)
                    for h in range(200)
                ],
            )
            for i in range(1, 4)
        ]
        result = _trend(curves)
        expected = _trend_naive(curves)
        assert result == expected


# ── Finding 2: digest baseline uses MAX(id) instead of MAX(captured_at) ───


class TestDigestBaselineMaxId:

    def test_tie_returns_one_row_per_publication(self, db):
        """Two snapshots with the same captured_at — only the higher-id row wins."""
        from app.models.content import Content, ContentStatus, ContentType
        from app.models.metrics import ContentMetric
        from app.models.project import Project
        from app.models.publication import Platform, Publication, PublicationStatus
        from app.models.user import User
        from app.security import hash_password
        from app.services.digest import _last_readings_before

        user = User(
            email="digest-perf@example.com",
            hashed_password=hash_password("hunter2hunter2"),
        )
        db.add(user)
        db.flush()

        project = Project(user_id=user.id, name="P", slug="p")
        db.add(project)
        db.flush()

        content = Content(
            project_id=project.id,
            title="Test",
            slug="test-digest-perf",
            status=ContentStatus.PUBLISHED,
            content_type=ContentType.TUTORIAL,
        )
        db.add(content)
        db.flush()

        pub = Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
        )
        db.add(pub)
        db.flush()

        shared_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        m1 = ContentMetric(
            publication_id=pub.id,
            captured_at=shared_time,
            views=100,
        )
        db.add(m1)
        db.flush()

        m2 = ContentMetric(
            publication_id=pub.id,
            captured_at=shared_time,
            views=105,
        )
        db.add(m2)
        db.flush()
        db.commit()

        moment = datetime(2025, 6, 2, tzinfo=timezone.utc)
        result = _last_readings_before(db, [pub.id], moment)

        assert len(result) == 1
        assert result[pub.id].id == m2.id
        assert result[pub.id].views == 105

    def test_empty_ids(self, db):
        from app.services.digest import _last_readings_before

        assert _last_readings_before(db, [], datetime.now(timezone.utc)) == {}

    def test_nothing_before_moment(self, db):
        """All snapshots are after moment — empty dict."""
        from app.models.content import Content, ContentStatus, ContentType
        from app.models.metrics import ContentMetric
        from app.models.project import Project
        from app.models.publication import Platform, Publication, PublicationStatus
        from app.models.user import User
        from app.security import hash_password
        from app.services.digest import _last_readings_before

        user = User(
            email="digest-perf2@example.com",
            hashed_password=hash_password("hunter2hunter2"),
        )
        db.add(user)
        db.flush()

        project = Project(user_id=user.id, name="P2", slug="p2")
        db.add(project)
        db.flush()

        content = Content(
            project_id=project.id,
            title="Test2",
            slug="test2-digest-perf",
            status=ContentStatus.PUBLISHED,
            content_type=ContentType.TUTORIAL,
        )
        db.add(content)
        db.flush()

        pub = Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
        )
        db.add(pub)
        db.flush()

        future = datetime(2025, 8, 1, tzinfo=timezone.utc)
        m = ContentMetric(publication_id=pub.id, captured_at=future, views=50)
        db.add(m)
        db.commit()

        moment = datetime(2025, 7, 1, tzinfo=timezone.utc)
        result = _last_readings_before(db, [pub.id], moment)
        assert result == {}
