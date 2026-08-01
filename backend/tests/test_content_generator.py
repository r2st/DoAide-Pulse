"""The content engine's contract: it always returns a draft, and never a lie.

The provider chain is stubbed so these run offline and deterministically.
"""
from __future__ import annotations

import pytest

from app.models.content import TARGET_WORDS, ContentType
from app.services import ai, content_generator, formats, llm_router
from app.services.github_client import Commit, RepoActivity


def good_payload(**overrides):
    body = "## Why\n\n" + ("word " * 300)
    return {
        "title": "Herald ships marketing automation",
        "body_markdown": body,
        "excerpt": "Herald writes the posts about the projects you ship.",
        "meta_description": "Herald automates developer marketing end to end, from "
        "repo watch to published post.",
        "keywords": ["marketing automation", "Marketing Automation", "devtools"],
        "tags": ["python", "fastapi", "ai", "devtools", "fifth"],
        "confidence": 0.85,
        **overrides,
    }


@pytest.fixture
def stub_llm(monkeypatch):
    """Make the chain return whatever the test sets, and record the request."""
    state = {"payload": good_payload(), "calls": []}

    def fake_complete(messages, *, model=None, temperature=0.7, max_tokens=1200, timeout=90.0):
        import json

        state["calls"].append({"model": model, "max_tokens": max_tokens})
        text = state["payload"]
        return llm_router.Completion(
            text=text if isinstance(text, str) else json.dumps(text),
            provider="stub",
            model=model or "stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    return state


def test_generation_normalizes_what_the_model_returns(project, stub_llm):
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)

    assert result.is_fallback is False
    assert result.provider == "stub"
    assert result.confidence == 0.85
    # Keywords deduped case-insensitively, and the project's own merged in.
    assert result.keywords.count("marketing automation") == 1
    assert "developer marketing" in result.keywords
    # Platform tags capped at four and stripped of spaces.
    assert len(result.tags) == 4
    assert all(" " not in tag for tag in result.tags)
    assert len(result.meta_description) <= 155


def test_token_budget_covers_the_reasoning_scratchpad(project, stub_llm):
    """Regression: a budget sized only to the prose comes back truncated.

    Measured against `gpt-oss-20b:free`, which spent 1487 of a 1490-token
    budget on reasoning and returned empty content.
    """
    content_generator.generate(project, ContentType.ANNOUNCEMENT)
    requested = stub_llm["calls"][-1]["max_tokens"]
    prose_only = int(TARGET_WORDS[ContentType.ANNOUNCEMENT] * 2.2)
    assert requested >= prose_only + 4000


def test_long_form_types_use_the_bigger_model(project, stub_llm, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "openrouter_model", "small")
    monkeypatch.setattr(settings, "openrouter_long_form_model", "big")

    content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert stub_llm["calls"][-1]["model"] == "small"

    content_generator.generate(project, ContentType.TUTORIAL)
    assert stub_llm["calls"][-1]["model"] == "big"


def test_chain_failure_falls_back_to_a_template(project, monkeypatch):
    monkeypatch.setattr(
        llm_router,
        "complete",
        lambda *a, **kw: (_ for _ in ()).throw(llm_router.AllProvidersFailed("down")),
    )
    result = content_generator.generate(project, ContentType.HOW_TO)

    assert result.is_fallback is True
    # Zero confidence is what keeps the autopilot from publishing it.
    assert result.confidence == 0.0
    assert result.body_markdown
    # And it says what it is, so nobody ships it by accident.
    assert "no AI provider was reachable" in result.body_markdown


def test_a_chain_of_thought_body_is_rejected(project, stub_llm):
    stub_llm["payload"] = good_payload(
        body_markdown="We need to write an announcement. The user wants a post about "
        "Herald. Let's write one that covers " + ("the features " * 60)
    )
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert result.is_fallback is True


def test_a_too_short_body_is_rejected(project, stub_llm):
    stub_llm["payload"] = good_payload(body_markdown="## Hi\n\nToo short to be a post.")
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert result.is_fallback is True


def test_a_missing_title_gets_a_sensible_one(project, stub_llm):
    stub_llm["payload"] = good_payload(title="")
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert result.is_fallback is False
    assert result.title == "Herald: Announcement"


def test_unparseable_output_falls_back(project, stub_llm):
    stub_llm["payload"] = "Here are my thoughts, but no JSON at all."
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert result.is_fallback is True


def test_repo_activity_reaches_the_prompt(project, monkeypatch):
    captured = {}

    def fake_complete(messages, **kwargs):
        import json

        captured["prompt"] = messages[-1]["content"]
        return llm_router.Completion(
            text=json.dumps(good_payload()), provider="stub", model="stub"
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)

    from datetime import UTC, datetime

    from app.services.github_client import Release

    activity = RepoActivity(
        full_name="r2st/Herald",
        new_commits=[
            Commit(
                sha=f"s{i}",
                message=f"feat: thing {i}\n\nlong body that should not appear",
                author="r2st",
                committed_at=datetime(2026, 7, 1, tzinfo=UTC),
                url="u",
            )
            for i in range(40)
        ],
        new_release=Release(
            tag="v1.2.0",
            name="Calendar",
            body="- Drag to reschedule",
            published_at=datetime(2026, 7, 20, tzinfo=UTC),
            url="u",
            prerelease=False,
        ),
    )
    content_generator.generate(project, ContentType.ANNOUNCEMENT, activity=activity)

    prompt = captured["prompt"]
    assert "v1.2.0" in prompt
    assert "Drag to reschedule" in prompt
    assert "40 new commit(s)" in prompt
    # Commits are capped and summarized to their first line.
    assert "showing the 25 most recent" in prompt
    assert "long body that should not appear" not in prompt


def test_instructions_reach_the_prompt(project, monkeypatch):
    captured = {}

    def fake_complete(messages, **kwargs):
        import json

        captured["prompt"] = messages[-1]["content"]
        return llm_router.Completion(
            text=json.dumps(good_payload()), provider="stub", model="stub"
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    content_generator.generate(
        project, ContentType.HOW_TO, instructions="Focus on the circuit breaker."
    )
    assert "Focus on the circuit breaker." in captured["prompt"]


def test_ideas_fall_back_without_a_provider(project):
    ideas = content_generator.suggest_ideas(project)
    assert ideas
    assert all(isinstance(idea.content_type, ContentType) for idea in ideas)


def test_ideas_prefer_a_release_when_there_is_one(project):
    from datetime import UTC, datetime

    from app.services.github_client import Release

    activity = RepoActivity(
        full_name="r2st/Herald",
        new_release=Release(
            tag="v2.0.0",
            name="Two",
            body="",
            published_at=datetime(2026, 7, 20, tzinfo=UTC),
            url="u",
            prerelease=False,
        ),
    )
    ideas = content_generator.suggest_ideas(project, activity=activity)
    assert ideas[0].content_type == ContentType.ANNOUNCEMENT
    assert "v2.0.0" in ideas[0].headline


def test_coercion_survives_a_model_that_returns_wrong_types(project, stub_llm):
    stub_llm["payload"] = good_payload(
        keywords="marketing automation, devtools",  # a string, not a list
        confidence="0.7",  # a string, not a float
        tags=["dev tools"],
    )
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)
    assert result.confidence == 0.7
    assert "marketing automation" in result.keywords
    assert result.tags == ["devtools"]


def test_ai_error_is_what_callers_catch():
    assert issubclass(ai.AIError, RuntimeError)


# --------------------------------------------------------------------------- #
# Shapes that are not articles                                                 #
# --------------------------------------------------------------------------- #


def _prompt_of(stub_llm_state, monkeypatch, project, content_type):
    """Generate once and hand back the user prompt the model was sent."""
    seen = {}

    def fake_complete(messages, *, model=None, temperature=0.7, max_tokens=1200, timeout=90.0):
        import json

        seen["prompt"] = messages[-1]["content"]
        return llm_router.Completion(
            text=json.dumps(stub_llm_state["payload"]), provider="stub", model="m"
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    content_generator.generate(project, content_type)
    return seen["prompt"]


def test_a_thread_prompt_asks_for_posts_rather_than_a_word_count(
    project, stub_llm, monkeypatch
):
    """A word count is what turns a thread into a blog post with line breaks."""
    prompt = _prompt_of(stub_llm, monkeypatch, project, ContentType.SOCIAL_THREAD)

    assert "one post per paragraph" in prompt
    assert "under 280 characters" in prompt
    assert "Length: about" not in prompt


def test_a_changelog_prompt_names_the_sections_it_may_use(
    project, stub_llm, monkeypatch
):
    prompt = _prompt_of(stub_llm, monkeypatch, project, ContentType.CHANGELOG)

    assert "Added, Changed, Deprecated, Removed, Fixed, Security" in prompt
    assert "No introduction, no conclusion" in prompt


def test_an_article_prompt_is_unchanged(project, stub_llm, monkeypatch):
    prompt = _prompt_of(stub_llm, monkeypatch, project, ContentType.TUTORIAL)

    assert 'Start at "## " for section headings' in prompt
    assert f"Length: about {TARGET_WORDS[ContentType.TUTORIAL]} words" in prompt


def test_an_over_long_post_is_repaired_rather_than_failing_the_generation(
    project, stub_llm
):
    """The whole point of the repair pass: a good thread with one long post."""
    stub_llm["payload"] = good_payload(
        body_markdown="A real hook that earns the thread.\n\n" + ("word " * 90)
    )

    result = content_generator.generate(project, ContentType.SOCIAL_THREAD)

    assert result.is_fallback is False
    posts = formats.parse_thread(result.body_markdown)
    assert len(posts) > 2
    assert all(len(post) <= formats.THREAD_POST_LIMIT for post in posts)


def test_a_models_numbering_is_stripped_before_storage(project, stub_llm):
    stub_llm["payload"] = good_payload(
        body_markdown="1/ First idea here.\n\n2/ Second idea here.\n\n3/ Third one."
    )

    result = content_generator.generate(project, ContentType.SOCIAL_THREAD)

    assert result.body_markdown == (
        "First idea here.\n\nSecond idea here.\n\nThird one."
    )


def test_a_changelogs_synonym_headings_are_canonicalised_on_the_way_in(
    project, stub_llm
):
    stub_llm["payload"] = good_payload(
        body_markdown="## Bug Fixes\n\n- The parser crash\n\n## New Features\n\n- RSS triggers"
    )

    result = content_generator.generate(project, ContentType.CHANGELOG)

    assert result.body_markdown == (
        "## Added\n\n- RSS triggers\n\n## Fixed\n\n- The parser crash"
    )


def test_a_short_changelog_is_not_rejected_for_being_short(project, stub_llm):
    """60 words is a sane floor for a blog post and absurd for a changelog."""
    stub_llm["payload"] = good_payload(body_markdown="## Fixed\n\n- The parser crash")

    result = content_generator.generate(project, ContentType.CHANGELOG)

    assert result.is_fallback is False
    assert result.body_markdown == "## Fixed\n\n- The parser crash"


def test_a_genuinely_empty_changelog_still_falls_back(project, stub_llm):
    stub_llm["payload"] = good_payload(body_markdown="Thanks for reading!")

    result = content_generator.generate(project, ContentType.CHANGELOG)

    assert result.is_fallback is True


def test_the_changelog_fallback_is_built_from_the_commits_themselves(
    project, monkeypatch
):
    """The one fallback worth publishing rather than merely worth rewriting."""
    monkeypatch.setattr(
        llm_router,
        "complete",
        lambda *a, **k: (_ for _ in ()).throw(ai.AIError("no providers")),
    )
    activity = RepoActivity(
        full_name="r2st/Herald",
        new_commits=[
            Commit(
                sha="a" * 40,
                message="feat: add RSS triggers",
                author="dev",
                committed_at=None,
                url="",
            ),
            Commit(
                sha="b" * 40,
                message="fix: stop the parser crash",
                author="dev",
                committed_at=None,
                url="",
            ),
        ],
    )

    result = content_generator.generate(
        project, ContentType.CHANGELOG, activity=activity
    )

    assert result.is_fallback is True
    assert "## Added" in result.body_markdown
    assert "- Add RSS triggers" in result.body_markdown
    assert "- Stop the parser crash" in result.body_markdown
    # No apology text: this one is publishable as it stands.
    assert "no AI provider" not in result.body_markdown


def test_the_thread_fallback_says_it_is_a_stub(project, monkeypatch):
    """A hook is a writing problem; there is no honest way to assemble one."""
    monkeypatch.setattr(
        llm_router,
        "complete",
        lambda *a, **k: (_ for _ in ()).throw(ai.AIError("no providers")),
    )

    result = content_generator.generate(project, ContentType.SOCIAL_THREAD)

    assert result.is_fallback is True
    assert "Rewrite before posting" in result.body_markdown
    posts = formats.parse_thread(result.body_markdown)
    assert all(len(post) <= formats.THREAD_POST_LIMIT for post in posts)
