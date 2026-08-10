"""Turning a model's answer into ideas, and what happens when it is not one.

``suggest_ideas`` is documented as never raising, which makes the parsing loop
the interesting part: every field is whatever the model felt like emitting, and
each malformed entry has to be dropped without taking the usable ones with it.

The other half is ``_fallback_ideas``, which is what actually runs on this
install most days — the free-tier keys exhaust their quota, the chain raises, and
the "obvious" ideas are the ones a user sees. They are worth being right.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentType
from app.models.trigger import TriggerKind
from app.services import ai, content_generator
from app.services.content_generator import _fallback_ideas, suggest_ideas
from app.services.signals import TriggerSignal

from .conftest import repo_activity


@pytest.fixture
def model_says(monkeypatch):
    """Make the chain return one JSON payload."""
    completion = type("C", (), {"provider": "openrouter", "model": "gpt-oss-20b:free"})()

    def _install(payload):
        monkeypatch.setattr(
            content_generator.ai,
            "json_completion",
            lambda *a, **kw: (payload, completion),
        )

    return _install


@pytest.fixture
def model_down(monkeypatch):
    def _down(*args, **kwargs):
        raise ai.AIError("All LLM providers failed")

    monkeypatch.setattr(content_generator.ai, "json_completion", _down)


# --------------------------------------------------------------------------- #
# Parsing what came back                                                       #
# --------------------------------------------------------------------------- #


def test_a_well_formed_answer_becomes_ideas(project, model_says):
    model_says(
        {
            "ideas": [
                {
                    "content_type": "tutorial",
                    "headline": "Retry logic that does not double-post",
                    "rationale": "It is the question everyone asks.",
                }
            ]
        }
    )

    ideas = suggest_ideas(project)

    assert len(ideas) == 1
    assert ideas[0].content_type == ContentType.TUTORIAL
    assert ideas[0].headline == "Retry logic that does not double-post"


def test_an_entry_that_is_not_an_object_is_dropped_without_taking_the_others(
    project, model_says
):
    model_says(
        {
            "ideas": [
                "just a string",
                None,
                ["a", "list"],
                {"content_type": "tutorial", "headline": "The real one", "rationale": "r"},
            ]
        }
    )

    ideas = suggest_ideas(project)

    assert [i.headline for i in ideas] == ["The real one"]


def test_an_entry_with_no_headline_is_dropped(project, model_says):
    """A headline is the whole idea; without one there is nothing to show."""
    model_says(
        {
            "ideas": [
                {"content_type": "tutorial", "rationale": "no headline here"},
                {"content_type": "tutorial", "headline": "", "rationale": "blank"},
                {"content_type": "tutorial", "headline": "Kept", "rationale": "r"},
            ]
        }
    )

    assert [i.headline for i in suggest_ideas(project)] == ["Kept"]


def test_a_content_type_the_model_invented_falls_back_rather_than_dropping_the_idea(
    project, model_says
):
    """The headline is the valuable part; a wrong type is worth keeping it for."""
    model_says(
        {"ideas": [{"content_type": "haiku", "headline": "Still useful", "rationale": "r"}]}
    )

    ideas = suggest_ideas(project)

    assert ideas[0].headline == "Still useful"
    assert ideas[0].content_type == ContentType.FEATURE_SPOTLIGHT


def test_a_content_type_in_the_wrong_case_is_still_recognised(project, model_says):
    model_says(
        {"ideas": [{"content_type": "How_To", "headline": "Cased oddly", "rationale": "r"}]}
    )

    assert suggest_ideas(project)[0].content_type == ContentType.HOW_TO


def test_more_ideas_than_asked_for_are_trimmed(project, model_says):
    model_says(
        {
            "ideas": [
                {"content_type": "tutorial", "headline": f"Idea {i}", "rationale": "r"}
                for i in range(10)
            ]
        }
    )

    assert len(suggest_ideas(project, limit=3)) == 3


@pytest.mark.parametrize("payload", [{}, {"ideas": None}, {"ideas": []}, {"other": 1}])
def test_an_answer_with_no_usable_ideas_falls_back_to_the_obvious_ones(
    project, model_says, payload
):
    model_says(payload)

    ideas = suggest_ideas(project)

    assert ideas, "an empty answer must not produce an empty list"


def test_every_entry_being_malformed_also_falls_back(project, model_says):
    model_says({"ideas": [{"rationale": "no headline"}, "string", 7]})

    assert suggest_ideas(project)


def test_a_dead_provider_chain_falls_back_rather_than_raising(
    project, model_down, caplog
):
    with caplog.at_level("INFO"):
        ideas = suggest_ideas(project)

    assert ideas
    assert "fell back" in caplog.text


# --------------------------------------------------------------------------- #
# The obvious ideas                                                            #
# --------------------------------------------------------------------------- #


def test_a_release_suggests_writing_about_the_release(project):
    ideas = _fallback_ideas(project, repo_activity(commits=3, release=True))

    announcement = next(i for i in ideas if i.content_type == ContentType.ANNOUNCEMENT)
    assert "v1.2.0" in announcement.headline
    assert "v1.2.0" in announcement.rationale


def test_commits_suggest_a_what_is_new_piece_and_say_how_many(project):
    ideas = _fallback_ideas(project, repo_activity(commits=7))

    spotlight = next(i for i in ideas if i.content_type == ContentType.FEATURE_SPOTLIGHT)
    assert "7 commits" in spotlight.rationale


def test_a_non_github_signal_reuses_its_own_headline_as_the_idea(project):
    """A webhook has no commits to count, but its headline already says what
    happened — which is what an idea is."""
    signal = TriggerSignal(
        kind=TriggerKind.WEBHOOK,
        source="Linear",
        headline="Cycle 14 closed with 31 issues shipped",
        summary="Details.",
        suggested_type=ContentType.ANNOUNCEMENT,
    )

    ideas = _fallback_ideas(project, None, signal)

    assert ideas[0].headline == "Cycle 14 closed with 31 issues shipped"
    assert ideas[0].content_type == ContentType.ANNOUNCEMENT
    assert "Linear" in ideas[0].rationale


def test_a_signal_carrying_no_news_does_not_become_an_idea(project):
    empty = TriggerSignal(kind=TriggerKind.WEBHOOK, source="Linear", headline="")

    ideas = _fallback_ideas(project, None, empty)

    assert [i.content_type for i in ideas] == [ContentType.HOW_TO]


def test_a_project_with_nothing_happening_still_gets_one_idea(project):
    """An empty list would render as "no ideas", which is not the same as
    "nothing has shipped lately"."""
    ideas = _fallback_ideas(project, None)

    assert len(ideas) == 1
    assert ideas[0].content_type == ContentType.HOW_TO
    assert project.name in ideas[0].headline
