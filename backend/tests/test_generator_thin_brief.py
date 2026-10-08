"""Generation against a project that has barely been filled in.

Every fixture project in the suite carries a full brief — stack, audience, URLs,
keywords — so each ``if brief[...]`` in the prompt builder and in the template
fallback has only ever been walked down its *true* arm. The project a user has
just created has none of those, and that is the brief the fallback exists for:
a thin record is exactly when no provider answering hurts most.

Also covers the two signal shapes the fallback reads: a firing with no headline,
and a GitHub scan that found neither a release nor a commit.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentType
from app.models.project import Project, Tone
from app.models.trigger import TriggerKind
from app.services import content_generator, signals
from app.services.signals import TriggerSignal

from .conftest import repo_activity


@pytest.fixture
def bare_project(db, user) -> Project:
    """A project with nothing on it but the two columns that cannot be empty."""
    row = Project(
        user_id=user.id,
        name="Sparse",
        slug="sparse",
        description="",
        repo_url="",
        live_url="",
        tech_stack=[],
        target_audience="",
        keywords=[],
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# The prompt builder                                                           #
# --------------------------------------------------------------------------- #


def test_a_thin_brief_contributes_no_optional_facts_to_the_prompt(bare_project):
    messages = content_generator._build_prompt(
        bare_project,
        ContentType.TUTORIAL,
        activity=None,
        instructions="",
    )

    prompt = messages[-1]["content"]
    assert "Project: Sparse" in prompt
    # The description line is always present; it says so when there is nothing.
    assert "(no description on file)" in prompt
    for absent in ("Built with:", "Who it is for:", "Live at:", "Source:",
                   "Target keywords:", "Extra direction"):
        assert absent not in prompt


def test_a_full_brief_contributes_every_optional_fact(project):
    messages = content_generator._build_prompt(
        project,
        ContentType.TUTORIAL,
        activity=None,
        instructions="  keep it short  ",
    )

    prompt = messages[-1]["content"]
    assert "Built with: FastAPI, React" in prompt
    assert "Who it is for: Indie developers" in prompt
    assert "Live at: https://pulse.example.com" in prompt
    assert "Source: https://github.com/r2st/DoAide-Pulse" in prompt
    assert "Target keywords: marketing automation, developer marketing" in prompt
    assert "Extra direction from the author: keep it short" in prompt


# --------------------------------------------------------------------------- #
# The template fallback                                                        #
# --------------------------------------------------------------------------- #


def test_the_article_fallback_omits_the_sections_a_thin_brief_cannot_fill(bare_project):
    # No provider keys in the test environment, so generate() takes the fallback.
    result = content_generator.generate(bare_project, ContentType.TUTORIAL)

    assert result.is_fallback is True
    assert result.confidence == 0.0
    assert "## What Sparse is" in result.body_markdown
    assert "Sparse is a work in progress." in result.body_markdown
    assert "## How it is built" not in result.body_markdown
    assert "## Try it" not in result.body_markdown
    # No keywords on the brief means nothing to focus on either.
    assert result.focus_keyword == ""
    assert result.keywords == []


def test_the_article_fallback_keeps_the_sections_a_full_brief_fills(project):
    result = content_generator.generate(project, ContentType.TUTORIAL)

    assert result.is_fallback is True
    assert "## How it is built" in result.body_markdown
    assert "## Try it" in result.body_markdown
    assert "https://pulse.example.com" in result.body_markdown


def test_the_thread_fallback_omits_the_link_when_there_is_no_live_url(bare_project):
    result = content_generator.generate(bare_project, ContentType.SOCIAL_THREAD)

    assert result.is_fallback is True
    assert "See it at" not in result.body_markdown
    assert "Rewrite before posting." in result.body_markdown


def test_a_changelog_with_prose_but_no_itemised_changes_still_gets_one_entry(bare_project):
    """A signal that says *something* happened but lists no changes.

    ``changelog_from_items`` has nothing to group, so the digest prose becomes
    the single entry. An empty changelog would be worse than a rough one: it
    reads as "nothing shipped this week", which is not what happened.
    """
    signal = TriggerSignal(
        kind=TriggerKind.WEBHOOK,
        source="Status page",
        headline="Deploy 41 went out",
        summary="Rolled the API forward to 0.4.1.",
        items=(),
        item_noun="change",
    )

    result = content_generator.generate(
        bare_project, ContentType.CHANGELOG, signal=signal
    )

    assert result.is_fallback is True
    assert "Deploy 41 went out" in result.body_markdown
    assert result.title == "Sparse — Deploy 41 went out"


def test_a_changelog_with_no_signal_at_all_falls_back_to_a_plain_title(bare_project):
    result = content_generator.generate(bare_project, ContentType.CHANGELOG)

    assert result.is_fallback is True
    assert result.title == "Sparse: changelog"


# --------------------------------------------------------------------------- #
# The signals the fallback reads                                               #
# --------------------------------------------------------------------------- #


def test_a_signal_with_no_headline_still_digests_its_summary():
    signal = TriggerSignal(
        kind=TriggerKind.RSS,
        source="Changelog feed",
        headline="",
        summary="Three fixes and a new endpoint.",
        items=("Fix the retry loop",),
        item_noun="entry",
    )

    digest = signal.digest()

    assert digest.splitlines()[0] == "Three fixes and a new endpoint."
    assert "- Fix the retry loop" in digest


def test_a_scan_that_found_nothing_produces_a_signal_with_no_news():
    signal = signals.from_repo_activity(repo_activity(commits=0, release=False))

    assert signal.has_news is False
    assert signal.headline == ""
    assert signal.digest() == ""
    assert signal.dedupe_key is None
