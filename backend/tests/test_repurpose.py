"""Repurposing: AI-drafted social snippets, with a mechanical fallback that
never fails even when every provider is down.
"""
from __future__ import annotations

import json

import pytest

from app.models.content import Content, ContentType
from app.services import llm_router, repurpose


def _long_content(**overrides) -> Content:
    body = (
        "## Why we built this\n\n"
        + ("This paragraph explains a real, specific feature of the product. " * 12)
        + "\n\n## How it works\n\n"
        + ("Another paragraph with concrete technical detail goes here. " * 12)
    )
    defaults = dict(
        project_id=1,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="Pulse ships internal link suggestions",
        slug="pulse-ships-internal-link-suggestions",
        body_markdown=body,
        excerpt="Pulse now suggests internal links by keyword overlap.",
        tags=["python", "fastapi"],
        canonical_url="https://example.com/pulse-ships-internal-link-suggestions",
    )
    defaults.update(overrides)
    return Content(**defaults)


@pytest.fixture
def stub_llm(monkeypatch):
    """Make the chain return whatever the test sets, and count calls."""
    state = {"payload": None, "calls": 0}

    def fake_complete(messages, *, model=None, fallback_models=(), **_kwargs):
        state["calls"] += 1
        text = state["payload"]
        return llm_router.Completion(
            text=text if isinstance(text, str) else json.dumps(text),
            provider="stub",
            model=model or "stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    return state


def test_falls_back_to_mechanical_builders_without_a_provider(project):
    """No provider key is configured in tests — the chain is empty."""
    content = _long_content(project_id=project.id)
    result = repurpose.generate(content, project)

    assert result.is_fallback is True
    assert result.provider is None
    assert 1 <= len(result.twitter_thread) <= 5
    assert all(len(t) <= 280 for t in result.twitter_thread)
    assert result.linkedin_post


def test_uses_ai_output_when_the_chain_succeeds(project, stub_llm):
    stub_llm["payload"] = {
        "twitter_thread": [
            "Pulse just shipped internal link suggestions for your posts.",
            "It scores every published piece by keyword overlap with the draft.",
            "No more guessing which old post to cross-link.",
        ],
        "linkedin_post": "Pulse now suggests internal links automatically, scoring "
        "every published post in a project by how much its keywords overlap with "
        "whatever you're currently drafting. One less thing to remember before you "
        "hit publish.",
    }
    content = _long_content(project_id=project.id)

    result = repurpose.generate(content, project)

    assert result.is_fallback is False
    assert result.provider == "stub"
    assert len(result.twitter_thread) == 3
    # The link is appended to the first tweet, not the rest.
    assert content.canonical_url in result.twitter_thread[0]
    assert content.canonical_url not in result.twitter_thread[1]
    assert result.linkedin_post.strip().startswith("Pulse now suggests")
    assert content.canonical_url in result.linkedin_post


def test_thin_source_skips_the_ai_call_entirely(project, stub_llm):
    """A source too short to say two different things doesn't spend a call."""
    content = _long_content(project_id=project.id, body_markdown="Just a few words here.")

    result = repurpose.generate(content, project)

    assert result.is_fallback is True
    assert stub_llm["calls"] == 0


def test_reasoning_leak_falls_back_instead_of_publishing_a_scratchpad(project, stub_llm):
    stub_llm["payload"] = {
        "twitter_thread": ["We need to write a thread about this feature first."],
        "linkedin_post": "The user wants a LinkedIn post about this feature.",
    }
    content = _long_content(project_id=project.id)

    result = repurpose.generate(content, project)

    assert result.is_fallback is True


def test_empty_ai_output_falls_back(project, stub_llm):
    stub_llm["payload"] = {"twitter_thread": [], "linkedin_post": ""}
    content = _long_content(project_id=project.id)

    result = repurpose.generate(content, project)

    assert result.is_fallback is True
