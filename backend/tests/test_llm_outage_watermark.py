"""What the autopilot does when no LLM provider answers at all.

The distinction under test is between a provider that answered badly and no
provider answering at all. The first is a finished piece of work that happens to
be poor — store the template, move on. The second is work that never started,
and the commits that would have been written about are still waiting.

Getting this wrong is expensive and silent: the free-tier keys Herald runs on
exhaust their daily quota most days, and the old behaviour stored a template,
advanced the watermark past the commits, and spent the day's content budget
doing it. Nothing failed loudly, and those commits never got a real post —
the next scan saw no news, because the watermark said it had already looked.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.content import Content, ContentIdea
from app.models.project import AutopilotMode
from app.services import ai, content_generator, content_pipeline
from app.tasks import autopilot_tasks

# Reused rather than rebuilt: `stub_github` fakes the GitHub read and points the
# task at the test's session, and `_share_session` (autouse) is what supplies it.
from tests.test_autopilot import (  # noqa: F401
    _share_session,
    make_activity,
    stub_github,
)


@pytest.fixture
def no_provider(monkeypatch):
    """Every provider unreachable — the chain raises before anything is written."""

    def _down(*args, **kwargs):
        raise ai.AIError("All LLM providers failed: openrouter 429; gemini 429")

    monkeypatch.setattr(content_generator.ai, "json_completion", _down)
    monkeypatch.setattr(content_generator.ai, "chat_completion", _down)


@pytest.fixture
def junk_provider(monkeypatch):
    """A provider answers, with a body too thin to be a post."""
    completion = type("C", (), {"provider": "openrouter", "model": "gpt-oss-20b:free"})()
    monkeypatch.setattr(
        content_generator.ai,
        "json_completion",
        lambda *a, **kw: ({"title": "Something", "body_markdown": "ok"}, completion),
    )


@pytest.fixture
def armed(db, project):
    """A project past its first scan, with commits worth writing about."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    return project


def test_a_total_outage_writes_nothing_and_holds_the_watermark(
    db, armed, stub_github, no_provider  # noqa: F811
):
    stub_github["set"](make_activity(commits=30, head="new-head"))

    result = autopilot_tasks.scan_project(armed.id)

    assert result["status"] == "llm_unavailable"
    # Nothing was written, and nothing was banked either — an idea generated
    # from the same dead chain is the same template text, and re-banking it on
    # every scan until the quota returns is how the ideas table fills with junk.
    assert db.query(Content).count() == 0
    assert db.query(ContentIdea).count() == 0
    # The watermark is what makes this recoverable: it still points at the
    # commit the scan started from, so the work is merely deferred.
    db.refresh(armed)
    assert armed.last_seen_commit_sha == "old"


def test_the_next_scan_writes_the_piece_the_outage_deferred(
    db, armed, stub_github, monkeypatch  # noqa: F811
):
    """The point of holding the watermark: the commits get their post later."""
    stub_github["set"](make_activity(commits=30, head="new-head"))

    def _down(*args, **kwargs):
        raise ai.AIError("All LLM providers failed")

    monkeypatch.setattr(content_generator.ai, "json_completion", _down)
    assert autopilot_tasks.scan_project(armed.id)["status"] == "llm_unavailable"

    # Quota resets; the very next sweep sees the same commits, not "no news".
    completion = type("C", (), {"provider": "groq", "model": "llama-3.3-70b"})()
    monkeypatch.setattr(
        content_generator.ai,
        "json_completion",
        lambda *a, **kw: (
            {
                "title": "Thirty commits of publish-path work",
                "body_markdown": "## What changed\n\n" + ("Real prose. " * 200),
                "excerpt": "What changed in the publish path.",
                "meta_description": "A round-up of the publish path work.",
                "keywords": ["herald"],
                "tags": ["python"],
                "confidence": 0.4,
            },
            completion,
        ),
    )

    result = autopilot_tasks.scan_project(armed.id)

    assert result["status"] == "queued_for_review"
    content = db.query(Content).one()
    assert content.source["fallback"] is False
    assert content.source["commit_count"] == 30
    db.refresh(armed)
    assert armed.last_seen_commit_sha == "new-head"


def test_an_outage_does_not_spend_the_daily_content_budget(
    db, armed, stub_github, no_provider, monkeypatch  # noqa: F811
):
    """A day of exhausted quota must not also cost the day's post allowance."""
    monkeypatch.setattr(settings, "autopilot_daily_content_limit", 1)
    stub_github["set"](make_activity(commits=30, head="new-head"))

    autopilot_tasks.scan_project(armed.id)

    assert autopilot_tasks._daily_count(db, armed.id) == 0


def test_a_provider_that_answers_badly_still_stores_its_template(
    db, armed, stub_github, junk_provider  # noqa: F811
):
    """The other half of the distinction, and the pre-existing behaviour.

    A provider *did* answer here. Asking again returns the same unusable body,
    so holding the watermark would stall this project forever — the template is
    stored, the watermark moves, and a human finds a stub in the review queue.
    """
    stub_github["set"](make_activity(commits=30, head="new-head"))

    result = autopilot_tasks.scan_project(armed.id)

    assert result["status"] == "queued_for_review"
    content = db.query(Content).one()
    assert content.source["fallback"] is True
    db.refresh(armed)
    assert armed.last_seen_commit_sha == "new-head"


def test_generate_names_the_cause_of_each_fallback(project, no_provider):
    """The signal the pipeline reads, at its source."""
    from app.models.content import ContentType

    generated = content_generator.generate(project, ContentType.ANNOUNCEMENT)

    assert generated.is_fallback is True
    assert generated.fallback_reason == content_generator.FALLBACK_NO_PROVIDER


def test_an_unusable_body_is_a_different_fallback(project, junk_provider):
    from app.models.content import ContentType

    generated = content_generator.generate(project, ContentType.ANNOUNCEMENT)

    assert generated.is_fallback is True
    assert generated.fallback_reason == content_generator.FALLBACK_UNUSABLE


def test_the_pipeline_refuses_to_route_what_was_never_written(
    db, project, no_provider
):
    """``generate_and_route`` raises rather than returning a routed stub."""
    from app.models.content import ContentType

    with pytest.raises(content_pipeline.GenerationUnavailable):
        content_pipeline.generate_and_route(
            db,
            project,
            content_type=ContentType.ANNOUNCEMENT,
            source={"kind": "autopilot"},
            defer_on_outage=True,
        )

    # Nothing reached the database — the caller is free to retry cleanly.
    assert db.query(Content).count() == 0


def test_a_caller_that_cannot_retry_still_gets_its_template(db, project, no_provider):
    """The default, and what an inbound webhook depends on.

    The request that carried this signal is gone by the time anyone could try
    again, so a stub in the review queue beats losing it.
    """
    from app.models.content import ContentType

    routed = content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "trigger"},
    )

    assert routed.is_fallback is True
    assert db.query(Content).count() == 1
