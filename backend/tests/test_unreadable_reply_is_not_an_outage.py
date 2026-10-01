"""A reply Pulse could not read is not the same as a provider that never came.

``ai.json_completion`` has two ways to fail and used to raise the same class for
both: the chain gave up (every key spent, every circuit open), or a provider
answered and the JSON envelope was not there. The commonest cause of the second
is a reply cut off at ``max_tokens`` with the object truncated mid-string —
which is precisely what a generation dying part-way through an article looks
like to the caller.

Only the first is transient. ``autopilot_tasks`` holds the repo watermark on a
transient failure, deliberately and rightly, so that the commits it did not
write about are still there next time. Pointing that at a chain that is *up*
turned the hold into a deadlock: the same commits, the same prompt, the same
unreadable answer, every scan, forever — no piece, no idea, no watermark
movement, and a GitHub read plus a model call spent on each pass. The only
symptom was a log line saying the LLM was unavailable while the LLM answered
every time.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentIdea, ContentType
from app.models.project import AutopilotMode
from app.services import ai, content_generator, content_pipeline
from app.tasks import autopilot_tasks

from .conftest import repo_activity as make_activity


class _Completion:
    """What the router hands back when a provider does answer."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.provider = "openrouter"
        self.model = "gpt-oss-20b:free"


@pytest.fixture
def armed(db, project):
    """A project past its first scan, with commits worth writing about."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    return project


@pytest.fixture
def truncated(monkeypatch):
    """A provider answers with the envelope cut off mid-article.

    The real shape of it: ``finish_reason: length`` on a reasoning model that
    spent most of the budget on its scratchpad, so the JSON opens, gets some way
    into ``body_markdown``, and simply stops.
    """
    cut = (
        '{"title": "Shipping the scheduler", "body_markdown": "## What changed\\n\\n'
        "The scheduler now picks a publish window from the project's own history "
        'rather than a fixed hour. That means'
    )

    def _answer(*_args, **_kwargs):
        return _Completion(cut)

    monkeypatch.setattr(content_generator.ai, "chat_completion_detailed", _answer)
    monkeypatch.setattr(ai, "chat_completion_detailed", _answer)


# --------------------------------------------------------------------------- #
# The distinction itself                                                       #
# --------------------------------------------------------------------------- #


def test_a_truncated_envelope_is_reported_as_a_reply_not_as_an_outage(truncated):
    with pytest.raises(ai.UnusableResponse) as caught:
        ai.json_completion([{"role": "user", "content": "hi"}])

    # Still an AIError, so every existing handler in the tree — the ones that
    # only want "no usable output" — keeps catching it unchanged.
    assert isinstance(caught.value, ai.AIError)
    # And it names the provider that answered, which is the whole point: an
    # outage has no provider to name.
    assert "openrouter" in str(caught.value)


def test_a_dead_chain_is_still_reported_as_an_outage(monkeypatch):
    def _down(*_args, **_kwargs):
        raise ai.AIError("All LLM providers failed: openrouter 429; gemini 429")

    monkeypatch.setattr(ai, "chat_completion_detailed", _down)

    with pytest.raises(ai.AIError) as caught:
        ai.json_completion([{"role": "user", "content": "hi"}])
    assert not isinstance(caught.value, ai.UnusableResponse)


def test_the_generator_marks_a_truncated_reply_unusable(project, truncated):
    generated = content_generator.generate(project, ContentType.ANNOUNCEMENT)

    assert generated.is_fallback
    # Not FALLBACK_NO_PROVIDER: a provider was reached and did reply. Asking it
    # again is the same call with the same budget, so the template is the final
    # answer rather than a placeholder to come back to.
    assert generated.fallback_reason == content_generator.FALLBACK_UNUSABLE


# --------------------------------------------------------------------------- #
# What that costs the autopilot                                                #
# --------------------------------------------------------------------------- #


def test_a_truncated_reply_stores_the_piece_and_moves_the_watermark(
    db, armed, stub_github, truncated
):
    stub_github["set"](make_activity(commits=30, head="new-head"))

    result = autopilot_tasks.scan_project(armed.id)

    assert result["status"] != "llm_unavailable"
    # The template is a poor post and a fine draft: confidence 0.0, so it can
    # never clear the auto-publish gate, and a human can open and rewrite it.
    piece = db.query(Content).one()
    assert piece.confidence == 0.0
    assert result["content_id"] == piece.id
    # The commits are consumed. That is the half that was missing — they had
    # been written about, however plainly, so claiming to have seen them is
    # true.
    db.refresh(armed)
    assert armed.last_seen_commit_sha == "new-head"


def test_the_scan_after_a_truncated_reply_does_not_re_read_the_same_commits(
    db, armed, stub_github, truncated
):
    """The deadlock, stated as the thing that ended it.

    Two scans over the same repo. The second asks GitHub what is new *since the
    head the first one saw*, so against a real repo it hears nothing and the
    commits are done with. Before this, both scans returned ``llm_unavailable``
    and the second asked from ``old`` again — the same question, forever, for as
    long as the model kept truncating.
    """
    stub_github["set"](make_activity(commits=30, head="new-head"))
    autopilot_tasks.scan_project(armed.id)

    autopilot_tasks.scan_project(armed.id)

    assert [call["since_sha"] for call in stub_github["calls"]] == ["old", "new-head"]


def test_a_total_outage_still_holds_the_watermark_and_writes_nothing(
    db, armed, stub_github, monkeypatch
):
    """The other side of the distinction, pinned here so it cannot drift back.

    The fix above narrows what counts as an outage; it must not narrow it to
    nothing.
    """

    def _down(*_args, **_kwargs):
        raise ai.AIError("All LLM providers failed: openrouter 429; gemini 429")

    monkeypatch.setattr(content_generator.ai, "json_completion", _down)
    monkeypatch.setattr(content_generator.ai, "chat_completion", _down)
    stub_github["set"](make_activity(commits=30, head="new-head"))

    result = autopilot_tasks.scan_project(armed.id)

    assert result["status"] == "llm_unavailable"
    assert db.query(Content).count() == 0
    assert db.query(ContentIdea).count() == 0
    db.refresh(armed)
    assert armed.last_seen_commit_sha == "old"


def test_a_truncated_reply_does_not_defer_a_caller_that_cannot_retry(
    db, project, truncated
):
    """``defer_on_outage`` is the autopilot's alone, and only for a real outage.

    A trigger passes the default and must never see ``GenerationUnavailable``;
    this pins that a truncated reply does not raise it for the caller that
    *does* ask to be deferred either.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()

    routed = content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "manual"},
        defer_on_outage=True,
    )

    assert routed.is_fallback
    assert routed.content.id is not None
