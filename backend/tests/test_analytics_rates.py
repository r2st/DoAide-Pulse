"""Derived rates and reading-time analysis.

The distinction under test throughout: a rate with nothing in its denominator
is ``None``, not ``0.0``. "Nobody clicked" and "this platform doesn't report
views" are different facts, and a dashboard that renders both as 0% tells the
user their best channel is dead.
"""
from __future__ import annotations

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import analytics_service


def _published(db, project, *, title, words=200, content_type=ContentType.TUTORIAL):
    row = Content(
        project_id=project.id,
        content_type=content_type,
        title=title,
        slug=title.lower().replace(" ", "-"),
        body_markdown="word " * words,
        status=ContentStatus.PUBLISHED,
    )
    db.add(row)
    db.flush()
    return row


def _publish_to(db, content, platform, **metrics):
    pub = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(pub)
    db.flush()
    if metrics:
        db.add(ContentMetric(publication_id=pub.id, **metrics))
    db.commit()
    return pub


# --------------------------------------------------------------------------- #
# Totals                                                                      #
# --------------------------------------------------------------------------- #


def test_rates_are_none_rather_than_zero_without_views():
    totals = analytics_service.Totals(content_count=1, published_count=1)
    assert totals.click_through_rate is None
    assert totals.read_rate is None
    assert totals.engagement_rate is None


def test_rates_are_fractions_of_views():
    totals = analytics_service.Totals(
        views=1000,
        reads=400,
        clicks=25,
        engagement=90,
        reads_reported=1,
        clicks_reported=1,
    )
    assert totals.click_through_rate == 0.025
    assert totals.read_rate == 0.4
    assert totals.engagement_rate == 0.09


def test_no_clicks_against_real_views_is_zero_not_none():
    """The one case where 0.0 is the honest answer: reported, and it was zero."""
    totals = analytics_service.Totals(views=500, clicks=0, clicks_reported=3)
    assert totals.click_through_rate == 0.0
    # Nothing reported at all is a different answer.
    assert analytics_service.Totals(views=500).click_through_rate is None


def test_to_dict_carries_the_derived_rates():
    """asdict() alone silently drops them — that is the bug this guards."""
    payload = analytics_service.Totals(
        views=200, clicks=10, reads=50, clicks_reported=1, reads_reported=1
    ).to_dict()
    assert payload["views"] == 200
    assert payload["click_through_rate"] == 0.05
    assert payload["read_rate"] == 0.25
    assert "engagement_rate" in payload


def test_totals_sum_reads_from_the_latest_snapshot(db, user, project):
    content = _published(db, project, title="Retry logic")
    pub = _publish_to(db, content, Platform.DEVTO)
    # Two snapshots: the second supersedes the first rather than adding to it.
    db.add(ContentMetric(publication_id=pub.id, views=100, reads=40, clicks=5))
    db.commit()
    db.add(ContentMetric(publication_id=pub.id, views=300, reads=120, clicks=15))
    db.commit()

    totals = analytics_service.totals(db, user.id)
    assert totals.views == 300
    assert totals.reads == 120
    assert totals.clicks == 15
    assert totals.read_rate == 0.4
    assert totals.click_through_rate == 0.05


# --------------------------------------------------------------------------- #
# Per-platform rates                                                          #
# --------------------------------------------------------------------------- #


def test_platform_rates_separate_reach_from_what_reach_is_worth(db, user, project):
    """A small platform whose readers click is not the same as a dead one."""
    big = _published(db, project, title="Big reach")
    small = _published(db, project, title="Small reach")
    _publish_to(db, big, Platform.DEVTO, views=10_000, reads=2_000, clicks=50)
    _publish_to(db, small, Platform.MASTODON, views=200, clicks=40)

    rows = {row["platform"]: row for row in analytics_service.by_platform(db, user.id)}

    assert rows["devto"]["views"] == 10_000
    assert rows["devto"]["click_through_rate"] == 0.005
    assert rows["devto"]["read_rate"] == 0.2
    # Fifty times the click-through on a fiftieth of the reach.
    assert rows["mastodon"]["click_through_rate"] == 0.2
    # Mastodon reports no reads at all — which is not a read rate of zero.
    assert rows["mastodon"]["read_rate"] is None


def test_a_platform_with_no_metrics_has_no_rates(db, user, project):
    content = _published(db, project, title="Nothing reported")
    _publish_to(db, content, Platform.GIT)

    rows = {row["platform"]: row for row in analytics_service.by_platform(db, user.id)}
    assert rows["git"]["published"] == 1
    assert rows["git"]["click_through_rate"] is None


def test_content_type_rows_carry_rates(db, user, project):
    content = _published(db, project, title="Type rates", content_type=ContentType.HOW_TO)
    _publish_to(db, content, Platform.DEVTO, views=400, clicks=20, reactions=8)

    row = analytics_service.by_content_type(db, user.id)[0]
    assert row["content_type"] == "how_to"
    assert row["clicks"] == 20
    assert row["click_through_rate"] == 0.05
    assert row["engagement_rate"] == 0.07


# --------------------------------------------------------------------------- #
# Engagement trend                                                            #
# --------------------------------------------------------------------------- #


def test_trend_carries_clicks_and_a_daily_rate(db, user, project):
    content = _published(db, project, title="Trending")
    _publish_to(db, content, Platform.DEVTO, views=500, clicks=25, reactions=10)

    trend = analytics_service.engagement_trend(db, user.id, days=7)
    today = trend[-1]
    assert today["views"] == 500
    assert today["clicks"] == 25
    assert today["click_through_rate"] == 0.05
    # A day with no snapshots has no rate — not a rate of zero.
    assert trend[0]["views"] == 0
    assert trend[0]["click_through_rate"] is None


# --------------------------------------------------------------------------- #
# Reading time                                                                #
# --------------------------------------------------------------------------- #


def test_read_time_is_empty_but_shaped_without_content(db, user):
    payload = analytics_service.read_time(db, user.id)
    assert payload["published_pieces"] == 0
    assert payload["avg_read_minutes"] is None
    assert payload["reader_minutes"] == 0
    assert [band["band"] for band in payload["by_length"]] == ["short", "medium", "long"]


def test_reader_minutes_counts_reads_not_views(db, user, project):
    """Counting the full reading time for a bounce invents attention."""
    content = _published(db, project, title="Long tutorial", words=2200)  # ~10 min
    _publish_to(db, content, Platform.DEVTO, views=1000, reads=100)

    payload = analytics_service.read_time(db, user.id)
    assert content.read_minutes == 10
    assert payload["reader_minutes"] == 1000  # 100 reads x 10 minutes
    assert payload["read_rate"] == 0.1
    assert payload["publications_reporting_reads"] == 1


def test_a_platform_that_never_reports_reads_leaves_the_basis_empty(db, user, project):
    """Zero reader-minutes here means "nobody counts", not "nobody read"."""
    content = _published(db, project, title="Mastodon only", words=1000)
    _publish_to(db, content, Platform.MASTODON, views=800)

    payload = analytics_service.read_time(db, user.id)
    assert payload["reader_minutes"] == 0
    assert payload["publications_reporting_reads"] == 0
    assert payload["read_rate"] is None


def test_pieces_land_in_the_band_their_length_earns(db, user, project):
    short = _published(db, project, title="Quick note", words=300)  # ~1 min
    medium = _published(db, project, title="Middling", words=1100)  # ~5 min
    long_ = _published(db, project, title="Deep dive", words=3000)  # ~14 min
    _publish_to(db, short, Platform.DEVTO, views=100, clicks=1)
    _publish_to(db, medium, Platform.DEVTO, views=200, clicks=10)
    _publish_to(db, long_, Platform.DEVTO, views=300, clicks=45)

    bands = {b["band"]: b for b in analytics_service.read_time(db, user.id)["by_length"]}

    assert bands["short"]["publications"] == 1
    assert bands["medium"]["publications"] == 1
    assert bands["long"]["publications"] == 1
    # The point of the breakdown: length is known before publishing, so a
    # click-through that climbs with it is a commissioning decision.
    assert bands["short"]["click_through_rate"] == 0.01
    assert bands["long"]["click_through_rate"] == 0.15
    assert bands["long"]["avg_views"] == 300.0


def test_an_empty_band_reports_nothing_rather_than_zero(db, user, project):
    content = _published(db, project, title="Only short", words=200)
    _publish_to(db, content, Platform.DEVTO, views=50)

    bands = {b["band"]: b for b in analytics_service.read_time(db, user.id)["by_length"]}
    assert bands["long"]["publications"] == 0
    assert bands["long"]["avg_views"] is None
    assert bands["long"]["avg_read_minutes"] is None


def test_averages_cover_published_content_only(db, user, project):
    _published(db, project, title="Published one", words=440)  # 2 min
    draft = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Enormous draft",
        slug="enormous-draft",
        body_markdown="word " * 10_000,
        status=ContentStatus.DRAFT,
    )
    db.add(draft)
    db.commit()

    payload = analytics_service.read_time(db, user.id)
    assert payload["published_pieces"] == 1
    assert payload["avg_read_minutes"] == 2.0
    assert payload["total_words"] == 440


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def test_read_time_endpoint(client, auth, db, project):
    content = _published(db, project, title="Endpoint piece", words=660)
    _publish_to(db, content, Platform.DEVTO, views=100, reads=30)

    resp = client.get("/api/v1/analytics/read-time", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["published_pieces"] == 1
    assert body["reader_minutes"] == 90  # 30 reads x 3 minutes
    assert body["read_rate"] == 0.3


def test_read_time_requires_authentication(client):
    assert client.get("/api/v1/analytics/read-time").status_code == 401


def test_overview_and_dashboard_expose_the_rates(client, auth, db, project):
    content = _published(db, project, title="Rated", words=440)
    _publish_to(db, content, Platform.DEVTO, views=1000, reads=250, clicks=40)

    overview = client.get("/api/v1/analytics/overview", headers=auth).json()
    assert overview["totals"]["click_through_rate"] == 0.04
    assert overview["totals"]["read_rate"] == 0.25
    assert overview["read_time"]["reader_minutes"] == 500

    dashboard = client.get("/api/v1/analytics/dashboard", headers=auth).json()
    assert dashboard["totals"]["click_through_rate"] == 0.04


def test_another_users_numbers_never_leak(client, auth, db, project, user):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="someone@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.flush()
    other_project = Project(
        user_id=stranger.id,
        name="Not yours",
        slug="not-yours",
        description="Someone else's project.",
        tone=Tone.TECHNICAL,
    )
    db.add(other_project)
    db.flush()
    theirs = _published(db, other_project, title="Their post", words=1000)
    _publish_to(db, theirs, Platform.DEVTO, views=99_999, reads=50_000, clicks=1_000)

    body = client.get("/api/v1/analytics/read-time", headers=auth).json()
    assert body["published_pieces"] == 0
    assert body["reader_minutes"] == 0
    assert analytics_service.totals(db, user.id).views == 0
