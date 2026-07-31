"""Analytics dashboard and overview endpoints.

Basic smoke tests — every analytics endpoint returns 200 and a sensible shape,
with and without data. The underlying aggregation logic is exercised by the
service tests; what we are checking here is that the router wires up to the
service correctly and that the response schemas don't crash on empty data.
"""
from __future__ import annotations

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus


def test_overview_empty(client, auth):
    resp = client.get("/api/v1/analytics/overview", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["content_count"] == 0
    assert body["totals"]["published_count"] == 0
    assert body["by_content_type"] == []
    assert body["by_platform"] == []
    assert isinstance(body["timeline"], list)


def test_dashboard_empty(client, auth):
    resp = client.get("/api/v1/analytics/dashboard", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["content_count"] == 0
    assert body["needs_review"] == 0
    assert body["failed_publications"] == []
    assert body["upcoming"] == []
    assert body["recent_content"] == []


def test_overview_with_content(client, auth, db, project):
    """With one published piece the counters reflect it."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="v1.0 is out",
        slug="v1-0-is-out",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(pub)
    db.commit()

    resp = client.get("/api/v1/analytics/overview", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["content_count"] == 1
    assert body["totals"]["published_count"] == 1


def test_dashboard_shows_review_and_failed(client, auth, db, project):
    review = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Guide draft",
        slug="guide-draft",
        status=ContentStatus.REVIEW,
    )
    failed_content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Failed post",
        slug="failed-post",
        status=ContentStatus.APPROVED,
    )
    db.add_all([review, failed_content])
    db.flush()
    failed_pub = Publication(
        content_id=failed_content.id,
        platform=Platform.MEDIUM,
        status=PublicationStatus.FAILED,
        error="401 Unauthorized",
    )
    db.add(failed_pub)
    db.commit()

    resp = client.get("/api/v1/analytics/dashboard", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_review"] == 1
    assert len(body["failed_publications"]) == 1
    assert body["failed_publications"][0]["error"] == "401 Unauthorized"


def test_analytics_requires_auth(client):
    assert client.get("/api/v1/analytics/overview").status_code == 401
    assert client.get("/api/v1/analytics/dashboard").status_code == 401


def test_engagement_trend_empty_covers_every_day(client, auth):
    resp = client.get("/api/v1/analytics/engagement-trend?days=7", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 8  # inclusive of both endpoints, like `timeline`
    assert all(day["views"] == 0 and day["engagement"] == 0 for day in body)


def test_engagement_trend_sums_snapshots_captured_on_the_same_day(
    client, auth, db, project
):
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow

    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Live post",
        slug="live-post",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    db.add(pub)
    db.flush()
    now = utcnow()
    db.add_all(
        [
            ContentMetric(
                publication_id=pub.id, captured_at=now, views=100, reactions=5, comments=2
            ),
            ContentMetric(
                publication_id=pub.id, captured_at=now, views=50, reactions=1, comments=0
            ),
        ]
    )
    db.commit()

    resp = client.get("/api/v1/analytics/engagement-trend?days=1", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    today = body[-1]
    assert today["views"] == 150
    assert today["engagement"] == 8  # (5+2) + (1+0)
    assert "engagement_trend" in client.get(
        "/api/v1/analytics/overview", headers=auth
    ).json()


def test_engagement_trend_days_out_of_range_is_rejected(client, auth):
    assert client.get(
        "/api/v1/analytics/engagement-trend?days=0", headers=auth
    ).status_code == 422
    assert client.get(
        "/api/v1/analytics/engagement-trend?days=181", headers=auth
    ).status_code == 422
