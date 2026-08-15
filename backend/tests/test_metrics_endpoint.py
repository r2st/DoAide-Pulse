"""``/api/v1/metrics`` — the operational read surface R79 left unbuilt.

Five docstrings described this endpoint before it existed. The tests here are
about the three things that are easy to get wrong once it does:

**It is authenticated.** The prior design had it open, and the rationale was
baked into ``llm_usage.summary``'s comments. Token spend and per-provider
failure rates are operational detail about the install; the 401 is asserted
first because it is the property most likely to be lost to a well-meant
"metrics should be scrapeable" patch.

**The account-scoped half really is scoped.** Content counts, scan frequency
and publish rates are filtered to the caller like every other read here. The
LLM and breaker halves cannot be — the usage table has no owner column — and
that asymmetry is asserted rather than left implicit, because it is the kind of
thing a reader assumes the other way.

**Nothing is zero that should be null.** A success rate of ``None`` and one of
``0.0`` are different findings, and a dashboard that renders the first as the
second reports a total failure where the truth is an absence of data.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.llm_usage import LLMUsage
from app.models.mixins import utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import llm_router, ops_metrics
from app.services.publishers import breaker as publishers_breaker


def _content(db, project, *, status=ContentStatus.DRAFT, title="A piece") -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{utcnow().timestamp()}",
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _publication(db, content, *, platform=Platform.DEVTO, status) -> Publication:
    row = Publication(content_id=content.id, platform=platform, status=status)
    db.add(row)
    db.commit()
    return row


def _usage(db, **kwargs) -> LLMUsage:
    row = LLMUsage(
        provider=kwargs.pop("provider", "groq"),
        model=kwargs.pop("model", "llama"),
        purpose=kwargs.pop("purpose", "content"),
        ok=kwargs.pop("ok", True),
        duration_ms=kwargs.pop("duration_ms", 1000),
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# Authentication                                                               #
# --------------------------------------------------------------------------- #


def test_metrics_needs_a_bearer_token(client):
    """The property the prior design would have given away."""
    assert client.get("/api/v1/metrics").status_code == 401


def test_a_bad_token_is_refused(client):
    assert (
        client.get(
            "/api/v1/metrics", headers={"Authorization": "Bearer not-a-token"}
        ).status_code
        == 401
    )


# --------------------------------------------------------------------------- #
# Content counts                                                               #
# --------------------------------------------------------------------------- #


def test_every_status_is_present_even_at_zero(client, auth, project):
    """A chart that drops its empty categories re-orders itself as data changes."""
    body = client.get("/api/v1/metrics", headers=auth).json()

    assert set(body["content"]["by_status"]) == {s.value for s in ContentStatus}
    assert body["content"]["total"] == 0


def test_content_is_counted_by_status(client, auth, db, project):
    _content(db, project, status=ContentStatus.DRAFT, title="one")
    _content(db, project, status=ContentStatus.DRAFT, title="two")
    _content(db, project, status=ContentStatus.PUBLISHED, title="three")

    body = client.get("/api/v1/metrics", headers=auth).json()

    assert body["content"]["by_status"]["draft"] == 2
    assert body["content"]["by_status"]["published"] == 1
    assert body["content"]["total"] == 3


def test_another_accounts_content_is_not_counted(client, auth, db, project):
    """The scoping, asserted rather than assumed."""
    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    their_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        repo_url="https://github.com/r2st/Theirs",
        tone=Tone.TECHNICAL,
    )
    db.add(their_project)
    db.commit()
    _content(db, their_project, status=ContentStatus.DRAFT, title="not yours")
    _content(db, project, status=ContentStatus.DRAFT, title="yours")

    body = client.get("/api/v1/metrics", headers=auth).json()

    assert body["content"]["total"] == 1
    assert [p["project_id"] for p in body["projects"]] == [project.id]


# --------------------------------------------------------------------------- #
# Scan frequency                                                               #
# --------------------------------------------------------------------------- #


def test_a_project_reports_its_scan_counters(client, auth, db, project):
    project.scan_count = 12
    project.last_scan_duration_ms = 850
    project.last_scanned_at = utcnow()
    db.commit()

    (row,) = client.get("/api/v1/metrics", headers=auth).json()["projects"]

    assert row["scan_count"] == 12
    assert row["last_scan_duration_ms"] == 850
    assert row["last_scanned_at"] is not None


def test_a_project_younger_than_a_day_reports_no_rate(client, auth, project):
    """Dividing two scans by a fraction of a day is an artefact, not a rate."""
    (row,) = client.get("/api/v1/metrics", headers=auth).json()["projects"]

    assert row["scans_per_day"] is None


def test_an_older_project_reports_a_rate(client, auth, db, project):
    project.created_at = utcnow() - timedelta(days=10)
    project.scan_count = 20
    db.commit()

    (row,) = client.get("/api/v1/metrics", headers=auth).json()["projects"]

    assert row["scans_per_day"] == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------------------------- #
# Publish rates                                                                #
# --------------------------------------------------------------------------- #


def test_a_success_rate_with_nothing_settled_is_null_not_zero(client, auth, project):
    """A fresh install has not got a rate of zero percent; it has not got one."""
    body = client.get("/api/v1/metrics", headers=auth).json()

    assert body["publishing"]["success_rate"] is None


def test_the_rate_counts_settled_rows_only(client, auth, db, project):
    """A queued row has not succeeded or failed yet.

    Counting it against the rate would make the number dip whenever something
    was queued and recover when it landed — motion with no information in it.
    """
    piece = _content(db, project)
    _publication(db, piece, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED)
    _publication(db, piece, platform=Platform.HASHNODE, status=PublicationStatus.FAILED)
    _publication(db, piece, platform=Platform.MEDIUM, status=PublicationStatus.PENDING)

    publishing = client.get("/api/v1/metrics", headers=auth).json()["publishing"]

    assert publishing["published"] == 1
    assert publishing["failed"] == 1
    assert publishing["in_flight"] == 1
    # One of two settled, not one of three.
    assert publishing["success_rate"] == 0.5


def test_publish_rates_break_down_per_platform(client, auth, db, project):
    piece = _content(db, project)
    _publication(db, piece, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED)
    _publication(db, piece, platform=Platform.HASHNODE, status=PublicationStatus.FAILED)

    by_platform = client.get("/api/v1/metrics", headers=auth).json()["publishing"][
        "by_platform"
    ]

    rates = {row["platform"]: row["success_rate"] for row in by_platform}
    assert rates[Platform.DEVTO.value] == 1.0
    assert rates[Platform.HASHNODE.value] == 0.0


# --------------------------------------------------------------------------- #
# LLM usage                                                                    #
# --------------------------------------------------------------------------- #


def test_token_spend_is_summed_over_the_window(client, auth, db, project):
    _usage(db, provider="groq", total_tokens=100, prompt_tokens=60, completion_tokens=40)
    _usage(db, provider="gemini", total_tokens=50, prompt_tokens=30, completion_tokens=20)

    llm = client.get("/api/v1/metrics", headers=auth).json()["llm"]

    assert llm["total_tokens"] == 150
    assert llm["calls"] == 2
    assert {p["provider"] for p in llm["by_provider"]} == {"groq", "gemini"}


def test_a_row_outside_the_window_is_not_counted(client, auth, db, project):
    old = _usage(db, total_tokens=999)
    old.created_at = utcnow() - timedelta(hours=48)
    db.commit()

    llm = client.get("/api/v1/metrics?hours=24", headers=auth).json()["llm"]

    assert llm["total_tokens"] == 0
    assert llm["calls"] == 0


def test_average_generation_time_comes_from_the_content_purpose(
    client, auth, db, project
):
    """Herald stores no generation duration, so this is the only honest source.

    The headline call is deliberately much slower: if the average were taken
    over every purpose it would be 3000, and the number would stop meaning
    "how long does a generation take".
    """
    _usage(db, purpose="content", duration_ms=1000)
    _usage(db, purpose="content", duration_ms=2000)
    _usage(db, purpose="headlines", duration_ms=9000)

    llm = client.get("/api/v1/metrics", headers=auth).json()["llm"]

    assert llm["avg_generation_ms"] == 1500
    assert llm["generations"] == 2


def test_no_generations_in_the_window_reports_null_not_zero(client, auth, db, project):
    """"The average generation took 0ms" is the one reading certainly wrong."""
    _usage(db, purpose="headlines", duration_ms=900)

    llm = client.get("/api/v1/metrics", headers=auth).json()["llm"]

    assert llm["avg_generation_ms"] is None
    assert llm["generations"] == 0


def test_a_failed_attempt_counts_towards_latency_but_not_tokens(
    client, auth, db, project
):
    """The reason ``summary``'s average counts refused attempts."""
    _usage(db, ok=True, duration_ms=1000, total_tokens=100)
    _usage(db, ok=False, duration_ms=90000, total_tokens=None)

    llm = client.get("/api/v1/metrics", headers=auth).json()["llm"]

    assert llm["calls_failed"] == 1
    assert llm["total_tokens"] == 100
    # The ninety-second timeout is visible, which is the whole point.
    assert llm["avg_duration_ms"] > 40000


def test_untagged_rows_are_grouped_rather_than_dropped(client, auth, db, project):
    """Purposes must sum to the total ``summary`` reports for the same window."""
    _usage(db, purpose="", total_tokens=10)
    _usage(db, purpose="content", total_tokens=20)

    llm = client.get("/api/v1/metrics", headers=auth).json()["llm"]

    assert sum(p["total_tokens"] for p in llm["by_purpose"]) == llm["total_tokens"]
    assert "" in {p["purpose"] for p in llm["by_purpose"]}


# --------------------------------------------------------------------------- #
# Breakers                                                                     #
# --------------------------------------------------------------------------- #


def test_an_open_breaker_is_reported(client, auth, project):
    """From this process — which the payload says, so a reader is not misled."""
    llm_router.breaker.reset()
    llm_router.breaker.open_for("groq", 300)
    try:
        breakers = client.get("/api/v1/metrics", headers=auth).json()["breakers"]

        assert "groq" in breakers["llm"]
        assert breakers["llm"]["groq"]["seconds_until_retry"] > 0
        assert breakers["scope"] == "api-process"
    finally:
        llm_router.breaker.reset()


def test_both_breaker_blocks_are_present_when_empty(client, auth, project):
    """An absent key and an empty one are otherwise indistinguishable."""
    llm_router.breaker.reset()
    publishers_breaker.reset()

    breakers = client.get("/api/v1/metrics", headers=auth).json()["breakers"]

    assert breakers["llm"] == {}
    assert breakers["publish"] == {}


# --------------------------------------------------------------------------- #
# The window                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hours", [0, -1, 721])
def test_an_out_of_range_window_is_refused(client, auth, project, hours):
    """Capped at the usage table's retention — past it the purge is the answer."""
    assert client.get(f"/api/v1/metrics?hours={hours}", headers=auth).status_code == 422


def test_the_window_is_echoed_back(client, auth, project):
    body = client.get("/api/v1/metrics?hours=48", headers=auth).json()

    assert body["window_hours"] == 48
    assert body["llm"]["window_hours"] == 48


# --------------------------------------------------------------------------- #
# The response model is a filter                                               #
# --------------------------------------------------------------------------- #


def test_metrics_model_does_not_drop_fields(client, auth, db, project):
    """Named in :mod:`app.schemas.metrics`, and the reason that docstring exists.

    FastAPI serialises through the response model, so a field the schema
    forgets is a field the endpoint silently stops returning. Adding a section
    to the service without adding it to the schema must fail here rather than
    quietly shrinking the payload.
    """
    piece = _content(db, project, status=ContentStatus.PUBLISHED)
    _publication(db, piece, status=PublicationStatus.PUBLISHED)
    _usage(db, total_tokens=100, purpose="content")
    project.scan_count = 3
    db.commit()

    produced = ops_metrics.build(db, project.user_id, hours=24)
    served = client.get("/api/v1/metrics", headers=auth).json()

    assert set(produced) == set(served), "the metrics model changed the top level"
    for section in ("content", "publishing", "llm", "breakers"):
        assert set(produced[section]) == set(served[section]), (
            f"metrics.{section}: the response model dropped or invented a field"
        )
    assert produced["projects"] and served["projects"]
    assert set(produced["projects"][0]) == set(served["projects"][0])
    assert set(produced["llm"]["by_provider"][0]) == set(served["llm"]["by_provider"][0])
    assert set(produced["llm"]["by_purpose"][0]) == set(served["llm"]["by_purpose"][0])
    assert set(produced["publishing"]["by_platform"][0]) == set(
        served["publishing"]["by_platform"][0]
    )
