"""The repo monitor and the decisions it makes.

GitHub is stubbed out with fabricated activity, so what is under test is the
policy — when Herald writes, when it stays quiet, and what it does with the
watermark — rather than the HTTP client. The stub itself is the ``stub_github``
fixture in ``conftest.py``, shared with the other modules that scan a repo.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus
from app.models.project import AutopilotMode, Project
from app.services import content_pipeline, github_client
from app.tasks import autopilot_tasks

from .conftest import repo_activity as make_activity


@pytest.fixture(autouse=True)
def _connected(connect):
    """Devto connected for every test in this file.

    The autopilot drops destinations the owner has no live connection for, so
    without this an `auto` project has nowhere to publish and *every* piece
    routes to review — which is what several tests below assert, for entirely
    different reasons. They would keep passing and stop testing their gate.
    """
    connect("devto")


def test_first_scan_only_baselines(db, project, stub_github):
    stub_github["set"](make_activity(commits=50, release=True))
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "baselined"
    assert db.query(Content).count() == 0
    # The watermark moved, so the next scan starts from here.
    db.refresh(project)
    assert project.last_seen_commit_sha == "abc123"


def test_a_handful_of_commits_is_below_the_threshold(db, project, stub_github):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=2))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "below_threshold"
    assert db.query(Content).count() == 0


def test_commits_below_the_threshold_are_not_consumed(db, project, stub_github):
    """Below the bar the watermark must not move — the commits are still news.

    The threshold means "not enough has happened *yet*". Advancing past commits
    it just refused makes it mean "not enough happened this hour", and since the
    scan runs hourly against a default of ten commits, a repo that pushes at any
    human rate is refused every hour and its commits are discarded every hour.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=2, head="two-in"))

    assert autopilot_tasks.scan_project(project.id)["status"] == "below_threshold"

    db.refresh(project)
    assert project.last_seen_commit_sha == "old"
    # We did look, even though we did nothing about it.
    assert project.last_scanned_at is not None


def test_commits_accumulate_across_scans_until_they_clear_the_bar(
    db, project, stub_github, monkeypatch
):
    """Three quiet hours and a busy one add up to a post."""
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    _modest_generation(monkeypatch)

    for _ in range(3):
        stub_github["set"](make_activity(commits=3, head="drip"))
        assert autopilot_tasks.scan_project(project.id)["status"] == "below_threshold"
        # Every scan still asks from the same point, because none of these
        # commits has been written about.
        assert stub_github["calls"][-1]["since_sha"] == "old"

    stub_github["set"](make_activity(commits=10, head="enough"))
    assert autopilot_tasks.scan_project(project.id)["status"] == "queued_for_review"

    db.refresh(project)
    assert project.last_seen_commit_sha == "enough"


def test_nothing_is_banked_for_commits_that_will_be_seen_again(
    db, project, stub_github
):
    """No ideas below the bar, or the held watermark re-banks them every hour."""
    from app.models.content import ContentIdea

    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()

    for _ in range(3):
        stub_github["set"](make_activity(commits=2))
        autopilot_tasks.scan_project(project.id)

    assert db.query(ContentIdea).count() == 0


def _modest_generation(monkeypatch):
    """A real generation the model is not confident about.

    Needed wherever the test is about *routing* rather than about the provider
    chain. No provider is configured in tests, so without a stub the autopilot
    now defers the scan entirely (see ``test_llm_outage_watermark``) and never
    reaches the decision under test.
    """
    from app.services.content_generator import GeneratedContent

    monkeypatch.setattr(
        autopilot_tasks.content_generator,
        "generate",
        lambda project, content_type, **kw: GeneratedContent(
            title="Herald 1.2.0 is out",
            body_markdown="## What changed\n\n" + ("Real prose. " * 200),
            excerpt="Herald 1.2.0 is out.",
            meta_description="Herald 1.2.0 is out, with a faster publish sweep.",
            keywords=["herald"],
            tags=["python"],
            confidence=0.2,
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
        ),
    )


def test_a_release_always_warrants_a_post(db, project, stub_github, monkeypatch):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _modest_generation(monkeypatch)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    content = db.query(Content).one()
    # A release becomes an announcement, and it waits for a human.
    assert content.content_type.value == "announcement"
    assert content.status == ContentStatus.REVIEW
    assert content.source["trigger"] == "release"
    assert content.source["release_tag"] == "v1.2.0"


def test_autopilot_off_banks_ideas_but_writes_nothing(db, project, stub_github):
    project.autopilot_mode = AutopilotMode.OFF
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=30, release=True))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "ideas_only"
    assert db.query(Content).count() == 0
    from app.models.content import ContentIdea

    assert db.query(ContentIdea).count() > 0


def _confident_generation(monkeypatch, body: str):
    """Make generation return a confident piece, so the auto gate is reachable.

    Without this every autopilot test sits at confidence 0.0 — no provider is
    configured — and never gets as far as the checks that follow it.
    """
    from app.services.content_generator import GeneratedContent

    monkeypatch.setattr(
        autopilot_tasks.content_generator,
        "generate",
        lambda project, content_type, **kw: GeneratedContent(
            title="Herald 1.2.0 is out",
            body_markdown=body,
            excerpt="Herald 1.2.0 is out.",
            meta_description="Herald 1.2.0 is out, with a faster publish sweep.",
            keywords=["herald"],
            tags=["python"],
            confidence=0.95,
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
        ),
    )


def test_a_confident_piece_with_a_dead_link_goes_to_review(
    db, project, stub_github, monkeypatch
):
    """An unreviewed publish is the one place a fabricated URL reaches readers."""
    from app.services import link_check

    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _confident_generation(monkeypatch, "See the [docs](https://example.com/invented).")
    monkeypatch.setattr(
        content_pipeline.link_check,
        "check_body",
        lambda body, **kw: [
            link_check.LinkStatus("https://example.com/invented", link_check.BROKEN, 404)
        ],
    )

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    assert result["dead_links"] == ["https://example.com/invented"]
    content = db.query(Content).one()
    assert content.status == ContentStatus.REVIEW
    # Recorded, so the review queue can say why a confident piece is waiting.
    assert content.source["dead_links"] == ["https://example.com/invented"]


def test_a_confident_piece_with_live_links_publishes(
    db, project, stub_github, monkeypatch
):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _confident_generation(monkeypatch, "See the [docs](https://example.com/real).")
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])
    # Bypass the SEO quality gate — the stub content is deliberately minimal.
    monkeypatch.setattr(content_pipeline.seo, "seo_score", lambda **kw: 100)
    # The publish itself is not what this test is about. Patched on the module
    # that calls it: `generate_and_route` resolves `publish_now` as its own
    # global, so an alias anywhere else would not intercept the dispatch.
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "auto_published"
    assert db.query(Content).one().source["dead_links"] == []


def test_a_confident_piece_with_a_weak_seo_score_goes_to_review(
    db, project, stub_github, monkeypatch
):
    """The second quality gate, exercised for real rather than bypassed.

    Every other autopilot test that reaches this far stubs ``seo.seo_score``
    to 100 — reasonably, since the stub bodies are not meant to be good SEO —
    but that leaves ``generate_and_route``'s own threshold check
    (``content_pipeline.py``, "held back from auto-publish: SEO score") never
    actually exercised end to end. This one lets the real scorer run against a
    body that is genuinely thin: no headings, no cover image, under 300
    words — the same kind of piece a confident model can produce about a
    one-line release.
    """
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _confident_generation(monkeypatch, "A short update about the release.")
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    assert result["seo_score"] < content_pipeline.seo.SEO_SCORE_THRESHOLD
    content = db.query(Content).one()
    assert content.status == ContentStatus.REVIEW
    assert content.source["seo_score"] == result["seo_score"]


def test_auto_mode_still_reviews_a_low_confidence_draft(
    db, project, stub_github, monkeypatch
):
    """Auto is permission to publish what the model stands behind, not everything."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _modest_generation(monkeypatch)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    assert db.query(Content).one().status == ContentStatus.REVIEW


def test_daily_limit_stops_a_busy_repo(db, project, stub_github, monkeypatch):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    monkeypatch.setattr(settings, "autopilot_daily_content_limit", 1)
    _modest_generation(monkeypatch)

    assert autopilot_tasks.scan_project(project.id)["status"] == "queued_for_review"

    project.last_seen_commit_sha = "older"
    db.commit()
    assert autopilot_tasks.scan_project(project.id)["status"] == "daily_limit_reached"


def test_rate_limit_leaves_the_watermark_alone(db, project, stub_github, monkeypatch):
    project.last_seen_commit_sha = "keep-me"
    db.commit()

    def raise_rate_limit(*args, **kwargs):
        raise github_client.GitHubRateLimited("slow down")

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", raise_rate_limit)

    assert autopilot_tasks.scan_project(project.id)["status"] == "rate_limited"
    db.refresh(project)
    # We did not actually look, so we must not claim to have seen anything.
    assert project.last_seen_commit_sha == "keep-me"


def test_project_without_a_repo_is_skipped(db, user, stub_github):
    project = Project(user_id=user.id, name="No repo", slug="no-repo")
    db.add(project)
    db.commit()
    assert autopilot_tasks.scan_project(project.id)["status"] == "no_repo"


def test_scanning_a_missing_project_does_not_raise(db, stub_github):
    assert autopilot_tasks.scan_project(9999)["status"] == "skipped"
