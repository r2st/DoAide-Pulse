"""Posts that are doing measurably worse than this user's own normal.

The dashboard already says what every post earned. What it cannot say is
whether a number is *bad*, because "412 views" means nothing without knowing
that this user's Dev.to posts usually take 1,800 in their first two days. That
comparison is the whole content of this module, and it only became possible
once :mod:`app.services.velocity` made the early windows readable.

Every alert here is relative to the user's own history on the same platform.
No industry benchmarks, no absolute thresholds: a hobby project's good week and
a popular library's bad one are the same number, and a tool that told the first
it was failing would be wrong in the way that gets a tool ignored.

Two things are worth an alert:

* **Underperforming** — the first ``velocity_benchmark_window_hours`` came in
  far under the platform's median. Early enough to still act on: a headline
  swap (:mod:`app.services.headlines`) or a re-share is worth trying on day
  three, not on day thirty.
* **Stalled** — a post that *did* land has stopped growing. Not a failure, and
  never urgent, but it is the list a refresh or an evergreen re-share should be
  drawn from.

Nothing here writes to the database or sends anything. It is a read-only
verdict the dashboard, the digest and the API all render.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models.publication import Platform
from app.services import velocity
from app.services.velocity import Curve

#: Ordering for the UI: warnings above notices, and within a kind the worst
#: ratio first. A list that puts a post at 4% of median below one at 45% is a
#: list nobody reads twice.
_SEVERITY_RANK = {"warning": 0, "info": 1}


@dataclass(frozen=True)
class Alert:
    """One thing worth looking at, and the numbers that justify saying so."""

    kind: str
    severity: str
    content_id: int
    publication_id: int
    platform: Platform
    title: str
    message: str
    #: Observed against expected, as a fraction. ``None`` for alerts that are
    #: not a comparison against a benchmark.
    ratio: float | None = None
    observed: int | None = None
    expected: float | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "content_id": self.content_id,
            "publication_id": self.publication_id,
            "platform": self.platform.value,
            "title": self.title,
            "message": self.message,
            "ratio": self.ratio,
            "observed": self.observed,
            "expected": self.expected,
        }


def _underperformance(group: list[Curve], curve: Curve) -> Alert | None:
    """Judge one post against its platform-mates, or decline to judge it."""
    window = float(settings.velocity_benchmark_window_hours)
    observed = curve.views_within(window)
    if observed is None:
        # Too young, or no poll covered the window. Both mean "ask again
        # later", not "this post is fine" and not "this post is bad".
        return None

    expected = velocity.benchmark_excluding(group, curve, hours=window)
    if not expected:
        # Either too few other posts to form a median, or a median of zero —
        # and a post cannot underperform a platform where nothing has ever
        # been read.
        return None

    ratio = round(observed / expected, 3)
    if ratio > settings.underperformance_threshold:
        return None

    return Alert(
        kind="underperforming",
        severity="warning",
        content_id=curve.content_id,
        publication_id=curve.publication_id,
        platform=curve.platform,
        title=curve.title,
        message=(
            f"{int(ratio * 100)}% of your usual first {int(window)} hours on "
            f"{curve.platform.value} — {observed:,} views against a median of "
            f"{expected:,.0f}. Worth trying a different headline while it is "
            f"still new."
        ),
        ratio=ratio,
        observed=observed,
        expected=expected,
    )


def _stalled(curve: Curve) -> Alert | None:
    window = float(settings.velocity_stall_window_hours)
    if not curve.is_stalled(window_hours=window, ratio=settings.velocity_stall_ratio):
        return None

    peak = curve.peak_gain(window) or 0
    recent = curve.recent_gain(window) or 0
    days = int(window / 24) or 1
    return Alert(
        kind="stalled",
        severity="info",
        content_id=curve.content_id,
        publication_id=curve.publication_id,
        platform=curve.platform,
        title=curve.title,
        message=(
            f"Growth has flattened: {recent:,} views in the last {days} "
            f"day{'s' if days != 1 else ''} against a best of {peak:,}. A "
            f"re-share or a refresh would find it a second audience."
        ),
        ratio=round(recent / peak, 3) if peak else None,
        observed=recent,
        expected=float(peak) if peak else None,
    )


def build(
    db: Session,
    user_id: int,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> list[Alert]:
    """Every alert for one user, worst first.

    At most one alert per publication: a post that underperformed *and* then
    stalled is one problem with two symptoms, and the earlier, more actionable
    one is the one to report.
    """
    all_curves = velocity.curves(db, user_id, now=now)
    grouped: dict[Platform, list[Curve]] = {}
    for curve in all_curves:
        grouped.setdefault(curve.platform, []).append(curve)

    out: list[Alert] = []
    for group in grouped.values():
        for curve in group:
            alert = _underperformance(group, curve) or _stalled(curve)
            if alert is not None:
                out.append(alert)

    out.sort(
        key=lambda a: (
            _SEVERITY_RANK.get(a.severity, 9),
            a.ratio if a.ratio is not None else 1.0,
        )
    )
    return out[:limit]


def summary(db: Session, user_id: int, *, limit: int = 10) -> dict:
    """Alerts plus the counts the dashboard badge needs."""
    found = build(db, user_id, limit=limit)
    return {
        "alerts": [a.as_dict() for a in found],
        "warnings": sum(1 for a in found if a.severity == "warning"),
        "notices": sum(1 for a in found if a.severity == "info"),
    }


__all__ = ["Alert", "build", "summary"]
