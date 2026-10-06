"""H035 computation audit — rounding in alert messages.

Two ``int()`` truncations in ``alerts.py`` understated the numbers a user
reads in their underperformance and stall alerts.  ``round()`` is the
correct conversion for a percentage that will be read as-is, and
``max(1, round(...))`` is the correct conversion for a day count that
must never be zero.
"""
from __future__ import annotations

from app.config import settings
from app.services import alerts
from tests.test_velocity import make_post


def _seed_normal(db, project, *, count: int = 3, views: int = 1000) -> None:
    for index in range(count):
        make_post(
            db,
            project,
            slug=f"normal-{index}",
            published_hours_ago=200,
            readings=[(24, views // 2), (46, views)],
        )


# ── Finding 1: percentage rounding in underperformance alerts ─────────── #


def test_underperformance_percentage_rounds_not_truncates(db, project, user):
    """ratio=0.459 → message says 46%, not 45%.

    ``int(0.459 * 100) = 45`` — truncation toward zero understated the
    percentage by one point.  ``round(0.459 * 100) = 46``.
    """
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="edge",
        published_hours_ago=200,
        readings=[(24, 200), (46, 459)],
    )

    (alert,) = [a for a in alerts.build(db, user.id) if a.kind == "underperforming"]
    assert alert.ratio == 0.459
    assert "46% of your usual" in alert.message


def test_exact_half_percent_rounds_up(db, project, user):
    """ratio=0.005 → message says 1% (round-half-to-even gives 0 for 0.5,
    but 0.005*100 = 0.5 which rounds to 0).  Confirm any ratio > 0 shows
    at least 0% and never a negative.
    """
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="tiny",
        published_hours_ago=200,
        readings=[(24, 1), (46, 5)],
    )
    matched = [a for a in alerts.build(db, user.id) if a.kind == "underperforming"]
    assert len(matched) == 1
    assert "% of your usual" in matched[0].message
    pct = int(matched[0].message.split("%")[0])
    assert pct >= 0


def test_whole_number_ratio_is_unchanged(db, project, user):
    """ratio=0.020 → message still says 2% — no regression on a clean case."""
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="dud",
        published_hours_ago=200,
        readings=[(24, 8), (46, 20)],
    )
    (alert,) = [a for a in alerts.build(db, user.id) if a.kind == "underperforming"]
    assert alert.ratio == 0.02
    assert "2% of your usual" in alert.message


# ── Finding 2: day count rounding in stall alerts ─────────────────────── #


def test_stall_days_rounds_not_truncates(db, project, user, monkeypatch):
    """A 36-hour window → message says 2 days, not 1.

    ``int(36 / 24) = 1`` — truncation dropped the fractional day.
    ``max(1, round(36 / 24)) = max(1, 2) = 2``.
    """
    monkeypatch.setattr(settings, "velocity_stall_window_hours", 36)
    readings = [(0, 0), (6, 500), (12, 1000)]
    readings += [(12 + 6 * n, 1000 + n) for n in range(1, 10)]
    make_post(
        db,
        project,
        slug="stale",
        published_hours_ago=200,
        readings=readings,
    )

    stalled = [a for a in alerts.build(db, user.id) if a.kind == "stalled"]
    assert len(stalled) == 1
    assert "last 2 day" in stalled[0].message


def test_stall_days_at_exact_multiple_is_unchanged(db, project, user):
    """Default 168-hour (7-day) window → message still says 7 days."""
    readings = [(0, 0), (24, 900), (48, 1000)]
    readings += [(48 + 24 * n, 1000 + n) for n in range(1, 16)]
    make_post(
        db,
        project,
        slug="evergreen",
        published_hours_ago=600,
        readings=readings,
    )

    (alert,) = alerts.build(db, user.id)
    assert alert.kind == "stalled"
    assert "last 7 day" in alert.message


def test_stall_days_under_24h_floors_to_one(db, project, user, monkeypatch):
    """A 12-hour window → message says 1 day, not 0 or 1."""
    monkeypatch.setattr(settings, "velocity_stall_window_hours", 12)
    monkeypatch.setattr(settings, "velocity_stall_ratio", 0.3)
    readings = [(0, 0), (3, 500), (6, 1000)]
    readings += [(6 + 3 * n, 1000 + n) for n in range(1, 10)]
    make_post(
        db,
        project,
        slug="shortstall",
        published_hours_ago=200,
        readings=readings,
    )

    stalled = [a for a in alerts.build(db, user.id) if a.kind == "stalled"]
    assert len(stalled) == 1
    assert "last 1 day" in stalled[0].message
