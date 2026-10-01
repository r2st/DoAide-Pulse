"""The gate on the prose, which is the one the auto-publish path did not have.

Pulse's auto-publish path had six gates before this one, and none of them read
the words. Confidence is the model's own opinion; ``seo.seo_score`` and
``seo.blocking_issues`` read the envelope; ``ai.stray_script_runs`` reads the
characters; ``factcheck`` reads the claims; ``dedup`` reads the piece against
its siblings. A fluent, well-structured, correctly-tagged four hundred words of
subordinate clauses at a reading ease of 20 clears every one of them and
publishes itself under the user's name.

Pulse already measures exactly that, and already refuses on it — it is the
DRAFT→REVIEW floor a *human* hits when they submit a piece by hand
(``content_quality_min_score``). So the gate a person had to clear to ask for a
reviewer's attention was stricter than the one Pulse cleared to publish
unread. These tests pin the gate that closes it, and the two things about it
that are easy to get wrong: it holds a piece back rather than discarding it,
and it banks its reasons whether or not it fired.

The floor is its own setting rather than the review one. See
``settings.autopilot_auto_publish_min_quality``: the two gates ask different
questions, and the one with nobody after it should be the stricter of the two.
"""
from __future__ import annotations

import json

import pytest

from app.models.content import ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.models.publication import Platform
from app.services import content_pipeline, llm_router, quality

#: Prose that is structurally perfect and unreadable: one clause piled on the
#: next, no fenced code, every SEO field present and correct. This is the shape
#: the gate exists for — nothing else in the pipeline objects to it.
DENSE_BODY = "## Overview\n\n" + (
    "The instantiation of the aforementioned architectural paradigm "
    "necessitates the comprehensive reconceptualisation of infrastructural "
    "dependencies whose interrelationships, notwithstanding their apparent "
    "orthogonality, constitute the substantive determinant of operational "
    "throughput characteristics. "
) * 12

#: The same envelope, written for a reader.
CLEAR_BODY = "## Overview\n\n" + (
    "Pulse watches your repo. When you ship, it writes the post. "
    "You review it, or you let it go out on its own. "
    "The draft lands in the queue in about a minute. "
    "Most teams edit the title and publish. "
) * 12


def _payload(body):
    return {
        "title": "Pulse ships marketing automation",
        "body_markdown": body,
        "excerpt": "Pulse writes the posts about the projects you ship.",
        "meta_description": "Pulse automates developer marketing end to end, "
        "from repo watch to published post.",
        "keywords": ["marketing automation", "devtools"],
        "tags": ["python", "fastapi"],
        "confidence": 0.95,
    }


@pytest.fixture
def writes(monkeypatch):
    """Make the provider chain return a piece with the given body."""

    def install(body):
        def fake_complete(messages, **kwargs):
            return llm_router.Completion(
                text=json.dumps(_payload(body)), provider="stub", model="stub-model"
            )

        monkeypatch.setattr(llm_router, "complete", fake_complete)

    return install


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, connect):
    """Every other gate held open, so what is under test here is this one alone.

    Dev.to connected for the same reason: without a destination the piece routes
    to review for want of somewhere to go, and the test passes without testing
    anything.
    """
    connect("devto")
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)
    monkeypatch.setattr(content_pipeline.seo, "SEO_SCORE_THRESHOLD", 0)
    monkeypatch.setattr(content_pipeline.settings, "link_check_enabled", False)


@pytest.fixture
def auto_project(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.DEVTO.value]
    db.commit()
    return project


def _route(db, project):
    return content_pipeline.generate_and_route(
        db, project, content_type=ContentType.ANNOUNCEMENT, source={"kind": "test"}
    )


# --------------------------------------------------------------------------- #
# The fixtures are what this file claims they are                              #
# --------------------------------------------------------------------------- #


def test_the_dense_body_is_the_thing_no_other_gate_objects_to():
    """If this drifts, every test below stops testing the gate.

    The point of the fixture is that it is *only* bad in the way this gate
    reads: unreadable prose in a structurally complete piece.
    """
    report = quality.report(
        title="Pulse ships marketing automation",
        body_markdown=DENSE_BODY,
        meta_description="Pulse automates developer marketing end to end, "
        "from repo watch to published post.",
        keywords=["marketing automation", "devtools"],
        focus_keyword="marketing automation",
        slug="herald-ships-marketing-automation",
    )

    assert report.score < 60, "the dense fixture must fail the default floor"
    assert report.readability.reading_ease is not None
    assert report.readability.reading_ease < 40
    assert report.code_ratio == 0.0, "this fixture is not about code density"


def test_the_clear_body_clears_the_floor():
    report = quality.report(
        title="Pulse ships marketing automation",
        body_markdown=CLEAR_BODY,
        meta_description="Pulse automates developer marketing end to end, "
        "from repo watch to published post.",
        keywords=["marketing automation", "devtools"],
        focus_keyword="marketing automation",
        slug="herald-ships-marketing-automation",
    )

    assert report.score >= 60


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #


def test_an_unreadable_piece_goes_to_review_instead_of_publishing_itself(
    db, auto_project, writes
):
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW
    assert routed.platforms == []


def test_the_piece_is_held_back_not_discarded(db, auto_project, writes):
    """Every gate above this one holds rather than bins, and for the same reason:
    the piece is written and paid for, and a human is the right resolution."""
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    assert routed.content.id is not None
    # Stripped, not equal: the generator trims the stored body. What matters
    # here is that the words survived the gate, not that whitespace did.
    assert routed.content.body_markdown.strip() == DENSE_BODY.strip()


def test_a_readable_piece_still_publishes_itself(db, auto_project, writes):
    """The other side of the line, so the gate cannot pass by refusing everything."""
    writes(CLEAR_BODY)

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.content.status == ContentStatus.APPROVED
    assert routed.platforms == [Platform.DEVTO.value]


def test_the_reason_is_banked_on_the_row(db, auto_project, writes):
    """A reviewer looking at a held piece should see why without reading the logs.

    The score alone is not actionable — told "quality 54" there is nothing to
    do. The two components are: a reading ease of 21 says shorten the
    sentences, a code ratio of 0.7 says write the glue.
    """
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    source = routed.content.source
    assert source["quality_score"] < 60
    assert source["reading_ease"] is not None
    assert "code_ratio" in source


def test_the_score_is_banked_even_when_the_gate_does_not_fire(
    db, auto_project, writes
):
    """Computed and stored whether or not it held the piece back — the same rule
    the SEO errors and the duplicate check follow."""
    writes(CLEAR_BODY)

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.content.source["quality_score"] >= 60
    assert routed.quality_score >= 60


def test_the_task_return_value_carries_the_score(db, auto_project, writes):
    """``summary()`` is what a Celery task returns and what an operator reads."""
    writes(DENSE_BODY)

    summary = _route(db, auto_project).summary()

    assert summary["quality_score"] < 60
    assert summary["status"] == content_pipeline.QUEUED_FOR_REVIEW


def test_the_log_line_names_both_components(db, auto_project, writes, caplog):
    writes(DENSE_BODY)

    with caplog.at_level("INFO"):
        _route(db, auto_project)

    assert "held" in caplog.text
    assert "quality score" in caplog.text
    assert "reading ease" in caplog.text


# --------------------------------------------------------------------------- #
# The threshold                                                                #
# --------------------------------------------------------------------------- #


def test_a_floor_of_zero_turns_the_gate_off(db, auto_project, writes, monkeypatch):
    """The documented way to switch this off without touching the review floor."""
    monkeypatch.setattr(
        content_pipeline.settings, "autopilot_auto_publish_min_quality", 0
    )
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED


def test_the_score_is_still_banked_when_the_gate_is_off(
    db, auto_project, writes, monkeypatch
):
    """Switching the gate off is about whether Pulse *acts* on the score, not
    about whether the reviewer gets to see it — the same rule ``factcheck_enabled``
    follows."""
    monkeypatch.setattr(
        content_pipeline.settings, "autopilot_auto_publish_min_quality", 0
    )
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    assert routed.content.source["quality_score"] < 60


def test_raising_the_floor_holds_back_a_piece_that_would_have_gone(
    db, auto_project, writes, monkeypatch
):
    monkeypatch.setattr(
        content_pipeline.settings, "autopilot_auto_publish_min_quality", 99
    )
    writes(CLEAR_BODY)

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW


def test_the_floor_is_refused_off_the_scale():
    """A 0-100 floor set to 140 stops every piece in the install at once, and the
    error would name a score nobody can reach."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(autopilot_auto_publish_min_quality=140)


# --------------------------------------------------------------------------- #
# What this gate is deliberately not                                           #
# --------------------------------------------------------------------------- #


def test_the_review_floor_is_a_different_number(db, auto_project, writes, monkeypatch):
    """Moving the auto-publish floor must not move the DRAFT→REVIEW floor.

    They are separate settings because they answer different questions, and a
    single number would mean tightening what publishes unread also tightens what
    a human is allowed to ask a reviewer to look at.
    """
    monkeypatch.setattr(
        content_pipeline.settings, "autopilot_auto_publish_min_quality", 100
    )
    monkeypatch.setattr(content_pipeline.settings, "content_quality_min_score", 0)
    writes(DENSE_BODY)

    routed = _route(db, auto_project)

    # Held back from publishing itself, and still allowed to sit in review.
    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW


def test_a_draft_project_is_unaffected(db, project, writes):
    """The gate only ever removes an auto-publish. A project on ``draft`` was
    never going to publish itself, so this must change nothing for it."""
    project.autopilot_mode = AutopilotMode.DRAFT
    project.autopilot_platforms = [Platform.DEVTO.value]
    db.commit()
    writes(DENSE_BODY)

    routed = _route(db, project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.source["quality_score"] < 60
