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


def test_engagement_trend_does_not_re_count_a_day_once_per_poll(
    client, auth, db, project
):
    """Two polls of one post on one day describe one post, not two.

    The counters the platforms report are cumulative, so the day's number is
    where the series ended, not the sum of every reading taken along the way.
    Summing them multiplied every day by the poll frequency.
    """
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
                publication_id=pub.id, captured_at=now, views=60, reactions=3, comments=1
            ),
            ContentMetric(
                publication_id=pub.id, captured_at=now, views=100, reactions=5, comments=2
            ),
        ]
    )
    db.commit()

    resp = client.get("/api/v1/analytics/engagement-trend?days=1", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    today = body[-1]
    assert today["views"] == 100  # where the series ended, not 60 + 100
    assert today["engagement"] == 7  # 5 + 2, not (3+1) + (5+2)
    assert "engagement_trend" in client.get(
        "/api/v1/analytics/overview", headers=auth
    ).json()


def test_engagement_trend_clamps_a_counter_that_went_backwards(
    client, auth, db, project
):
    """A purge or a rescrape must not read as a day the audience shrank."""
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow

    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Rescraped",
        slug="rescraped",
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
            ContentMetric(publication_id=pub.id, captured_at=now, views=100),
            ContentMetric(publication_id=pub.id, captured_at=now, views=40),
        ]
    )
    db.commit()

    today = client.get(
        "/api/v1/analytics/engagement-trend?days=1", headers=auth
    ).json()[-1]
    assert today["views"] == 100


def test_engagement_trend_weights_reads_by_the_piece_they_belong_to(
    client, auth, db, project
):
    """Reader-minutes are per-piece, not the day's reads times an average.

    Two posts of very different lengths read on the same day: 10 reads of a
    one-minute post and 2 reads of a long one. Multiplying 12 reads by the
    average length would report attention nobody paid.
    """
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow

    now = utcnow()
    for slug, words, reads in (("short-one", 100, 10), ("long-one", 2200, 2)):
        content = Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            title=slug,
            slug=slug,
            body_markdown=" ".join(["word"] * words),
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
        db.flush()
        db.add(
            ContentMetric(
                publication_id=pub.id, captured_at=now, views=100, reads=reads
            )
        )
    db.commit()

    body = client.get("/api/v1/analytics/engagement-trend?days=1", headers=auth).json()
    today = body[-1]
    # 100 words -> 1 minute (floored), 2200 words -> 10 minutes.
    assert today["reads"] == 12
    assert today["reader_minutes"] == 10 * 1 + 2 * 10
    assert all(day["reader_minutes"] == 0 for day in body[:-1])


def test_engagement_trend_reader_minutes_ignore_platforms_that_dont_count_reads(
    client, auth, db, project
):
    """A NULL read is unknown, not zero, and contributes nothing."""
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow

    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Toot",
        slug="toot",
        body_markdown=" ".join(["word"] * 2200),
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.MASTODON,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(pub)
    db.flush()
    db.add(
        ContentMetric(
            publication_id=pub.id, captured_at=utcnow(), views=500, reads=None
        )
    )
    db.commit()

    today = client.get(
        "/api/v1/analytics/engagement-trend?days=1", headers=auth
    ).json()[-1]
    assert today["views"] == 500
    assert today["reader_minutes"] == 0


def test_read_time_attributes_reader_minutes_to_the_length_band(
    client, auth, db, project
):
    """The band breakdown carries its own share of the reading time."""
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow

    for slug, words, reads in (("brief", 100, 30), ("epic", 3300, 4)):
        content = Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            title=slug,
            slug=slug,
            body_markdown=" ".join(["word"] * words),
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
        db.flush()
        db.add(
            ContentMetric(
                publication_id=pub.id, captured_at=utcnow(), views=200, reads=reads
            )
        )
    db.commit()

    body = client.get("/api/v1/analytics/read-time", headers=auth).json()
    bands = {band["band"]: band for band in body["by_length"]}
    # 1-minute piece read 30 times; 15-minute piece read 4 times.
    assert bands["short"]["reader_minutes"] == 30
    assert bands["long"]["reader_minutes"] == 60
    assert bands["medium"]["reader_minutes"] == 0
    # The bands account for every minute the headline figure claims.
    assert sum(b["reader_minutes"] for b in body["by_length"]) == body["reader_minutes"]


def test_engagement_trend_days_out_of_range_is_rejected(client, auth):
    assert client.get(
        "/api/v1/analytics/engagement-trend?days=0", headers=auth
    ).status_code == 422
    assert client.get(
        "/api/v1/analytics/engagement-trend?days=181", headers=auth
    ).status_code == 422
