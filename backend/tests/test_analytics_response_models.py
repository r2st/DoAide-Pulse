"""A response model is a filter, so an incomplete one is a silent outage.

The nine analytics endpoints returned bare ``dict``, which documented nothing:
a generated client got ``Any`` for every dashboard payload and ``/docs`` showed
an empty box where the interesting half should be. Giving them models fixes
that and introduces a failure mode the bare dict did not have — FastAPI
serialises *through* the model, so a field the model forgets is a field the API
stops returning, with no error anywhere and a chart that quietly goes blank.

So completeness is asserted rather than reviewed. ``populated`` builds an
account with something in every branch these payloads have — two projects,
published and failed and scheduled publications, a review-queue draft, metric
snapshots on two days, pieces short and long enough to land in different length
bands — and each test below diffs what the service produced against what the
endpoint answered with. A key on one side and not the other fails, and the
failure names the key.

The rate fields get their own test. ``None`` and ``0.0`` mean different things
throughout analytics — "no platform you publish to reports this" against
"nobody did it" — and a model that coerced the first into the second would pass
a key diff while destroying the distinction the aggregation exists to make.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import alerts, analytics_service, digest, velocity


@pytest.fixture
def populated(db, user, project):
    """An account with something in every branch of the analytics payloads."""
    now = utcnow()

    second = Project(
        user_id=user.id,
        name="Second",
        slug="second",
        description="A second project, so by_project has more than one row.",
    )
    db.add(second)
    db.flush()

    # Two published pieces of very different lengths, so more than one length
    # band is occupied and `avg_read_minutes` has something to average.
    short = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Short one",
        slug="short-one",
        status=ContentStatus.PUBLISHED,
        body_markdown="word " * 200,
        published_at=now - timedelta(days=2),
    )
    long_form = Content(
        project_id=second.id,
        content_type=ContentType.TUTORIAL,
        title="Long one",
        slug="long-one",
        status=ContentStatus.PUBLISHED,
        body_markdown="word " * 4000,
        published_at=now - timedelta(days=1),
    )
    # One waiting on a human, so `needs_review` is not zero.
    db.add_all(
        [
            short,
            long_form,
            Content(
                project_id=project.id,
                content_type=ContentType.TUTORIAL,
                title="Waiting",
                slug="waiting",
                status=ContentStatus.REVIEW,
            ),
        ]
    )
    db.flush()

    published = Publication(
        content_id=short.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=now - timedelta(days=2),
        external_url="https://dev.to/someone/short-one",
    )
    # A second platform on the other piece, so by_platform has two rows.
    also_published = Publication(
        content_id=long_form.id,
        platform=Platform.HASHNODE,
        status=PublicationStatus.PUBLISHED,
        published_at=now - timedelta(days=1),
    )
    failed = Publication(
        content_id=long_form.id,
        platform=Platform.MEDIUM,
        status=PublicationStatus.FAILED,
        error="Medium said no.",
    )
    scheduled = Publication(
        content_id=short.id,
        platform=Platform.MASTODON,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=now + timedelta(days=1),
    )
    db.add_all([published, also_published, failed, scheduled])
    db.flush()

    # Two readings per publication on different days: a trend is a difference
    # between snapshots, so one reading produces no day-over-day gain at all.
    db.add_all(
        [
            ContentMetric(
                publication_id=published.id,
                captured_at=now - timedelta(days=1),
                views=40,
                reads=10,
                clicks=2,
                reactions=3,
                comments=1,
            ),
            ContentMetric(
                publication_id=published.id,
                captured_at=now,
                views=120,
                reads=35,
                clicks=9,
                reactions=8,
                comments=3,
            ),
            # No reads or clicks reported here — the branch that makes a rate
            # null rather than zero.
            ContentMetric(
                publication_id=also_published.id,
                captured_at=now - timedelta(hours=6),
                views=15,
                reactions=1,
            ),
            ContentMetric(
                publication_id=also_published.id,
                captured_at=now,
                views=55,
                reactions=4,
            ),
        ]
    )
    db.commit()
    return user


def _keys(value) -> set[str]:
    """The keys of *value*, or of its first element if it is a list."""
    if isinstance(value, list):
        return set(value[0]) if value else set()
    return set(value)


def _assert_same_keys(produced, served, where: str) -> None:
    dropped = _keys(produced) - _keys(served)
    invented = _keys(served) - _keys(produced)

    assert not dropped, f"{where}: the response model drops {sorted(dropped)}"
    assert not invented, f"{where}: the response model invents {sorted(invented)}"


# --------------------------------------------------------------------------- #
# Nothing is dropped                                                           #
# --------------------------------------------------------------------------- #


def test_the_fixture_populates_every_branch(client, auth, populated):
    """A guard on the fixture. Empty payloads would make every diff below vacuous."""
    body = client.get("/api/v1/analytics/overview", headers=auth).json()

    assert body["by_content_type"], "no content types"
    assert body["by_platform"], "no platforms"
    assert len(body["by_project"]) >= 2, "only one project"
    assert body["top_content"], "no top content"
    assert body["read_time"]["by_length"], "no length bands"

    dashboard = client.get("/api/v1/analytics/dashboard", headers=auth).json()
    assert dashboard["failed_publications"], "no failed publications"
    assert dashboard["upcoming"], "nothing scheduled"
    assert dashboard["recent_content"], "no recent content"
    assert dashboard["needs_review"], "nothing in review"


def test_overview_keeps_every_field_the_service_produced(client, auth, db, populated):
    produced = analytics_service.overview(db, populated.id)
    served = client.get("/api/v1/analytics/overview", headers=auth).json()

    _assert_same_keys(produced, served, "overview")
    for section in produced:
        _assert_same_keys(produced[section], served[section], f"overview.{section}")

    _assert_same_keys(
        produced["read_time"]["by_length"],
        served["read_time"]["by_length"],
        "overview.read_time.by_length",
    )


def test_engagement_trend_keeps_every_field(client, auth, db, populated):
    produced = analytics_service.engagement_trend(db, populated.id)
    served = client.get("/api/v1/analytics/engagement-trend", headers=auth).json()

    _assert_same_keys(produced, served, "engagement-trend")
    assert len(served) == len(produced)


def test_read_time_keeps_every_field(client, auth, db, populated):
    produced = analytics_service.read_time(db, populated.id)
    served = client.get("/api/v1/analytics/read-time", headers=auth).json()

    _assert_same_keys(produced, served, "read-time")
    _assert_same_keys(
        produced["by_length"], served["by_length"], "read-time.by_length"
    )


def test_velocity_summary_keeps_every_field(client, auth, db, populated):
    produced = velocity.summary(db, populated.id)
    served = client.get("/api/v1/analytics/velocity", headers=auth).json()

    _assert_same_keys(produced, served, "velocity")
    for section in ("benchmarks", "fastest", "stalled"):
        _assert_same_keys(produced[section], served[section], f"velocity.{section}")


def test_alerts_keeps_every_field(client, auth, db, populated):
    produced = alerts.summary(db, populated.id)
    served = client.get("/api/v1/analytics/alerts", headers=auth).json()

    _assert_same_keys(produced, served, "alerts")


def test_digest_keeps_every_field(client, auth, db, user, populated):
    produced = digest.build(db, user).as_dict()
    served = client.get("/api/v1/analytics/digest", headers=auth).json()

    _assert_same_keys(produced, served, "digest")
    for section in ("published", "top", "failed", "upcoming", "attention"):
        _assert_same_keys(produced[section], served[section], f"digest.{section}")
    _assert_same_keys(produced["movement"], served["movement"], "digest.movement")


def test_dashboard_keeps_every_field(client, auth, db, populated):
    """No service function to diff against — the handler builds this one itself.

    So the keys are named here. That is the point: the dashboard payload is
    assembled inline, which is exactly where a field is easiest to add on one
    side of the model and not the other.
    """
    served = client.get("/api/v1/analytics/dashboard", headers=auth).json()

    assert set(served) == {
        "totals",
        "needs_review",
        "failed_publications",
        "upcoming",
        "recent_content",
        "by_project",
        "timeline",
        "alerts",
    }
    assert set(served["failed_publications"][0]) == {
        "id",
        "content_id",
        "platform",
        "error",
    }
    assert set(served["upcoming"][0]) == {
        "id",
        "content_id",
        "title",
        "platform",
        "scheduled_for",
    }
    assert set(served["recent_content"][0]) == {
        "id",
        "title",
        "status",
        "content_type",
        "project_id",
        "project_name",
        "created_at",
    }
    _assert_same_keys(
        analytics_service.totals(db, populated.id).to_dict(),
        served["totals"],
        "dashboard.totals",
    )


def test_the_velocity_curve_keeps_its_configuration_keyed_windows(
    client, auth, db, populated
):
    """``views_first_{n}h`` is named from a setting, so no field can declare it.

    ``VelocityCurveOut`` allows extras for exactly this. A strict model would
    drop both counts, which is the whole content of the velocity panel, and
    every test above would still pass because they compare the outer keys.
    """
    from app.config import settings

    produced = velocity.summary(db, populated.id)
    served = client.get("/api/v1/analytics/velocity", headers=auth).json()

    early = f"views_first_{int(settings.velocity_early_window_hours)}h"
    benchmark = f"views_first_{int(settings.velocity_benchmark_window_hours)}h"

    assert early in produced["fastest"][0]
    assert early in served["fastest"][0]
    assert benchmark in served["fastest"][0]


def test_the_velocity_curve_detail_keeps_its_points(client, auth, db, populated):
    curve = velocity.summary(db, populated.id)["fastest"][0]

    served = client.get(
        f"/api/v1/analytics/velocity/{curve['publication_id']}", headers=auth
    ).json()

    assert set(served["points"][0]) == {"hours", "views", "engagement"}
    _assert_same_keys(curve, {k: v for k, v in served.items() if k != "points"}, "curve")


# --------------------------------------------------------------------------- #
# Null is not zero                                                             #
# --------------------------------------------------------------------------- #


def test_an_unreported_rate_stays_null_through_the_model(client, auth, db, populated):
    """The distinction the whole aggregation exists to make.

    Hashnode reported views and nothing else in the fixture, so its read rate
    is unknown rather than nought. A model that defaulted these to 0.0 would
    pass every key diff above and tell the reader that nobody read anything.
    """
    served = client.get("/api/v1/analytics/overview", headers=auth).json()
    hashnode = next(
        row for row in served["by_platform"] if row["platform"] == "hashnode"
    )

    assert hashnode["read_rate"] is None
    assert hashnode["click_through_rate"] is None
    # And the platform that did report is a number, so "null" is not simply
    # what this endpoint always says.
    devto = next(row for row in served["by_platform"] if row["platform"] == "devto")
    assert isinstance(devto["read_rate"], float)


def test_a_reported_rate_of_zero_is_still_zero(client, auth, db, user, project):
    """The other side of it: nought reported is not the same as nothing reported."""
    now = utcnow()
    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Nobody clicked",
        slug="nobody-clicked",
        status=ContentStatus.PUBLISHED,
        published_at=now,
    )
    db.add(content)
    db.flush()
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=now,
    )
    db.add(pub)
    db.flush()
    db.add(
        ContentMetric(publication_id=pub.id, captured_at=now, views=100, clicks=0)
    )
    db.commit()

    served = client.get("/api/v1/analytics/overview", headers=auth).json()

    assert served["totals"]["click_through_rate"] == 0.0
    assert served["totals"]["read_rate"] is None


# --------------------------------------------------------------------------- #
# What the schema now says                                                     #
# --------------------------------------------------------------------------- #


def test_no_analytics_endpoint_still_answers_an_untyped_object():
    """The gap this closes, asserted where it was: nine endpoints, no schema.

    Twelve now — the three dashboard endpoints (published series, top content,
    platform breakdown) were added after; generation-cost was removed (cross-
    tenant leak — token spend is install-wide, served via /metrics). The count is the
    guard on the walk, so it moves when the surface does; what must not move is
    ``untyped``.
    """
    from fastapi.routing import APIRoute

    from app.main import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
                continue
            included = getattr(route, "original_router", None)
            yield from walk(getattr(included, "routes", None) or getattr(route, "routes", []))

    analytics_routes = [
        route
        for route in walk(create_app().routes)
        if "analytics" in (route.tags or [])
    ]

    assert len(analytics_routes) == 12
    untyped = [r.path for r in analytics_routes if r.response_model is None]
    assert untyped == []


def test_the_models_reach_the_generated_schema(client):
    """A model that is declared but not referenced documents nothing."""
    spec = client.get("/openapi.json").json()
    overview = spec["paths"]["/api/v1/analytics/overview"]["get"]
    schema_ref = overview["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema_ref["$ref"].endswith("OverviewOut")
    assert "OverviewOut" in spec["components"]["schemas"]
    assert "TotalsOut" in spec["components"]["schemas"]
