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
  far under the platform's median, and the post is still under
  ``underperformance_max_age_hours`` old. Early enough to still act on: a
  headline swap (:mod:`app.services.headlines`) or a re-share is worth trying
  on day three, not on day thirty. The age bound is what makes that sentence
  true rather than merely intended — without it the panel is a standing list of
  the worst posts an account has ever published, sorted so that the ones
  nothing can be done about crowd out the one that went out this week.
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
        """The wire shape of one alert. ``platform`` goes out as its value."""
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
    """Judge one post against its platform-mates, or decline to judge it.

    Only while the post is new enough for the answer to be worth anything. The
    verdict itself never expires — a weak first two days in March is still a
    weak first two days — but the alert is not a verdict, it is a prompt to do
    something, and the something it names stops working once the post has left
    the feeds. A post old enough to be past that is handed to :func:`_stalled`
    by the caller, which offers the remedy that does still apply.
    """
    if curve.age_hours > float(settings.underperformance_max_age_hours):
        return None

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


def _stalled(curve: Curve, facts: velocity.StallFacts | None) -> Alert | None:
    """Judge one post against its own best week, from *facts* rather than its tail.

    *facts* is ``None`` for a publication that has never been polled, which has
    no growth to have stopped.
    """
    window = float(settings.velocity_stall_window_hours)
    if facts is None or not facts.stalled(
        curve.age_hours, window_hours=window, ratio=settings.velocity_stall_ratio
    ):
        return None

    peak = facts.peak_gain or 0
    recent = facts.recent_gain or 0
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
    stalled is one problem with two symptoms, and only the symptom something
    can still be done about is worth reporting. Age decides which that is —
    :func:`_underperformance` declines once a post is too old for a headline to
    change its distribution, which is the point at which :func:`_stalled`'s
    remedy, a re-share, becomes the one on offer.

    **Nothing here reads a whole series.** Both halves of the pass are bounded,
    and they are bounded differently because they ask different questions:

    * :func:`_underperformance` only ever asks about the first
      ``velocity_benchmark_window_hours`` of a post's life — its own, and every
      platform-mate's, since the median is drawn from the same window. So the
      curves are built with ``within_hours``, and a post polled every six hours
      for a year contributes four readings instead of some 1,400.
    * :func:`_stalled` asks about the end, which no prefix can see. It gets its
      two numbers from :func:`velocity.stall_facts` instead: three queries for
      the whole account, one row per publication each.

    This used to read every snapshot on the account, and the docstring here
    argued that it had to. The argument was that ``summary``'s trick —
    bracketing the stall verdict between two aggregates, see
    :func:`velocity._stall_verdict` — cannot work for an alert, because an alert
    quotes the post's ``peak_gain`` in its message and a bracket only settles
    the yes/no. That part was true and still is. What was wrong was the sentence
    after it: that computing ``peak_gain`` in SQL needs a ``RANGE`` frame over an
    interval, "which Postgres has and SQLite does not". SQLite has taken a
    numeric ``RANGE`` offset since 3.28, and ordering by seconds rather than by
    a timestamp gives both dialects the same frame — which is all
    :func:`velocity.peak_gains` needed to exist. The rows this reads no longer
    grow with the age of the account.

    The answers did not change, and :mod:`tests.test_alerts_bounded` asserts that
    against the unbounded computation rather than against a fixture.
    """
    all_curves = velocity.curves(
        db,
        user_id,
        now=now,
        # Everything `_underperformance` and `benchmark_excluding` ask of a
        # curve is a question about this window. `_stalled` asks about the tail
        # and is answered from `stall_facts` below, not from these.
        within_hours=float(settings.velocity_benchmark_window_hours),
    )
    facts = velocity.stall_facts(
        db, all_curves, window_hours=float(settings.velocity_stall_window_hours)
    )
    grouped: dict[Platform, list[Curve]] = {}
    for curve in all_curves:
        grouped.setdefault(curve.platform, []).append(curve)

    out: list[Alert] = []
    for group in grouped.values():
        for curve in group:
            alert = _underperformance(group, curve) or _stalled(
                curve, facts.get(curve.publication_id)
            )
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
