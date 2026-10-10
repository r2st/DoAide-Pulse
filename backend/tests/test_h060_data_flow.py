"""H060 — M2 data-flow computation bugs.

Bug 1: ``engagement_alerts._latest_totals`` joined on
``max(captured_at)`` which is not unique — two metrics captured in the
same second (sweep + refresh-button race) would both match, doubling
the engagement total. Fixed by joining on ``max(id)`` instead.

Bug 2: ``llm_usage._weighted_mean`` returned ``0`` when no calls had
been recorded. The identical function in ``ops_metrics`` returns
``None``, and the comment there explains why: 0 ms shows as the fastest
possible install on a dashboard, while ``None`` renders as "no data".
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import engagement_alerts, llm_usage


# ------------------------------------------------------------------ #
# Bug 1 — _latest_totals double-count on same-second captures        #
# ------------------------------------------------------------------ #


def test_same_second_metrics_are_not_double_counted(db, user):
    """Two ContentMetric rows with identical captured_at must not both
    contribute to the engagement total."""
    project = Project(user_id=user.id, name="p", slug="p")
    db.add(project)
    db.flush()

    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.PUBLISHED,
        title="t",
        slug="t",
    )
    db.add(content)
    db.flush()

    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="ext-1",
    )
    db.add(pub)
    db.flush()

    same_instant = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    m1 = ContentMetric(
        publication_id=pub.id,
        captured_at=same_instant,
        views=100,
        reactions=5,
        comments=2,
        clicks=0,
        shares=0,
    )
    m2 = ContentMetric(
        publication_id=pub.id,
        captured_at=same_instant,
        views=100,
        reactions=5,
        comments=2,
        clicks=0,
        shares=0,
    )
    db.add_all([m1, m2])
    db.commit()

    totals = engagement_alerts._latest_totals(db, [content.id])

    engagement, views, platforms = totals[content.id]
    # Only the latest row (highest id) should count — not both.
    assert engagement == 7, f"expected 7 (one row), got {engagement} (double-counted)"
    assert views == 100


# ------------------------------------------------------------------ #
# Bug 2 — llm_usage._weighted_mean returns 0 instead of None         #
# ------------------------------------------------------------------ #


def test_weighted_mean_returns_none_when_no_calls():
    """An install with no LLM calls should report None, not 0."""
    result = llm_usage._weighted_mean([])
    assert result is None, f"expected None for no calls, got {result}"


def test_weighted_mean_returns_value_when_calls_exist():
    """Normal case: the weighted mean is computed correctly."""
    providers = [
        {"provider": "a", "calls": 10, "avg_duration_ms": 200},
        {"provider": "b", "calls": 30, "avg_duration_ms": 400},
    ]
    result = llm_usage._weighted_mean(providers)
    # (200*10 + 400*30) / 40 = 14000/40 = 350
    assert result == 350


def test_weighted_mean_returns_none_with_zero_call_providers():
    """Providers present but all with zero calls → None."""
    providers = [
        {"provider": "a", "calls": 0, "avg_duration_ms": 0},
    ]
    result = llm_usage._weighted_mean(providers)
    assert result is None
