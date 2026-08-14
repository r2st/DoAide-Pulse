"""What ``confidence`` means, and what happens when the model answers differently.

The field gates unreviewed publishing: at or above
``AUTOPILOT_AUTO_PUBLISH_CONFIDENCE`` the autopilot posts without a human
looking, below it the piece goes to the review queue. The prompt asks for
0.0–1.0 and says what the number is for, and most of the time that is what comes
back.

The rest of the time it does not, and the direction of the failure is what these
tests pin down. Free models answer the same question on a 1–10 or 0–100 scale
often enough to matter, and reading ``3`` — a model saying *do not publish this*
— as the top of the scale is the worst available outcome: it takes the piece the
model was least sure of and publishes it unreviewed. Anything off the documented
scale is therefore treated as no answer at all, which routes to review.
"""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.models.content import ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.models.publication import Platform
from app.services import ai, content_generator, content_pipeline, llm_router


def _payload(confidence):
    return {
        "title": "Herald ships marketing automation",
        "body_markdown": "## Why\n\n" + ("word " * 300),
        "excerpt": "Herald writes the posts about the projects you ship.",
        "meta_description": "Herald automates developer marketing end to end, "
        "from repo watch to published post.",
        "keywords": ["marketing automation", "devtools"],
        "tags": ["python", "fastapi"],
        "confidence": confidence,
    }


@pytest.fixture
def answers(monkeypatch):
    """Make the provider chain return a payload with the given confidence."""

    def install(confidence):
        def fake_complete(messages, **kwargs):
            return llm_router.Completion(
                text=json.dumps(_payload(confidence)),
                provider="stub",
                model="stub-model",
            )

        monkeypatch.setattr(llm_router, "complete", fake_complete)

    return install


# --------------------------------------------------------------------------- #
# The coercion itself                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [0.0, 0.5, 0.79, 0.8, 1.0])
def test_a_value_on_the_scale_is_taken_as_written(value):
    assert ai.as_float(value, default=0.5) == value


@pytest.mark.parametrize(
    "value",
    [
        3,  # a 1-10 scale
        8.5,  # a 1-10 scale, again
        95,  # a percentage
        100,  # a percentage at the top
        -1,  # below the floor
        float("nan"),  # the least readable answer of all
    ],
)
def test_a_value_off_the_scale_is_discarded_not_clamped(value):
    """The old behaviour clamped, which turned ``3`` into maximum confidence."""
    assert ai.as_float(value, default=0.5) == 0.5


def test_a_numeric_string_on_the_scale_still_works():
    """Models quote numbers about a third of the time. That is not the failure."""
    assert ai.as_float("0.72", default=0.5) == 0.72


# --------------------------------------------------------------------------- #
# Through the generator                                                       #
# --------------------------------------------------------------------------- #


def test_an_off_scale_confidence_generates_a_piece_but_not_a_confident_one(
    project, answers
):
    answers(7)
    result = content_generator.generate(project, ContentType.ANNOUNCEMENT)

    # The post itself is fine — this is not a failed generation, just an
    # unreadable self-assessment.
    assert result.is_fallback is False
    assert result.title == "Herald ships marketing automation"
    assert result.confidence == 0.5
    assert result.confidence < settings.autopilot_auto_publish_confidence


# --------------------------------------------------------------------------- #
# Through the autopilot, which is what the number is actually for              #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _connected(connect):
    """Devto connected, so the confidence gate is the only thing that can stop a
    publish here. Without it the piece routes to review for want of a
    destination and the test passes without testing anything."""
    connect("devto")


@pytest.fixture
def auto_project(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.DEVTO.value]
    db.commit()
    return project


def test_a_confidence_of_seven_does_not_auto_publish(db, auto_project, answers):
    """The regression this exists for: 7 clamped to 1.0 and went out unreviewed."""
    answers(7)

    routed = content_pipeline.generate_and_route(
        db,
        auto_project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW
    assert routed.platforms == []


def test_a_genuine_high_confidence_still_auto_publishes(db, auto_project, answers, monkeypatch):
    """The other side of the line, so the fix cannot pass by refusing everything."""
    # The SEO gate is a separate decision and has its own tests; hold it open so
    # what is under test here is the confidence gate alone.
    monkeypatch.setattr(content_pipeline.seo, "SEO_SCORE_THRESHOLD", 0)
    monkeypatch.setattr(content_pipeline.settings, "link_check_enabled", False)
    # The worker opens its own session against the production engine — see
    # test_autopilot for the same stub.
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)
    answers(0.95)

    routed = content_pipeline.generate_and_route(
        db,
        auto_project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )

    assert routed.confidence == 0.95
    assert routed.status == content_pipeline.AUTO_PUBLISHED
