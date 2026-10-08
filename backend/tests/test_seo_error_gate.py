"""An ``error``-level SEO issue holds a piece back however it scored.

The autopilot had one SEO gate — ``seo_score`` against ``SEO_SCORE_THRESHOLD``
— and it asks the wrong question about a broken piece. The score is a judgement
about quality, denominated in points a strong post can absorb: a missing meta
description costs fifteen, and the cover image an automated piece never has
costs five more. An otherwise-immaculate piece with no meta description at all
therefore lands on exactly 70, against a ``< 70`` comparison, and published
itself.

``SeoIssue.level`` already draws the line the gate needed — "error" means a
platform or a crawler will visibly mishandle the post — and nothing was reading
it. These tests pin the arithmetic that made the hole (so a re-tuning of the
deductions cannot quietly close and reopen it), the gate that now covers it, and
that the reason survives onto the row a reviewer looks at.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.services import content_generator, content_pipeline, seo
from app.services.content_generator import GeneratedContent

# Long enough to clear the 300-word check, and mentioning the focus keyword
# often enough to sit in the scoring band — so the only thing wrong with the
# pieces below is the one thing each test removes.
_BODY = (
    "## Retries in Pulse\n\n"
    "Retries in Pulse are the part people ask about first. "
    + "The retry path is careful about retries and about what a retry costs. " * 45
)

_CLEAN = {
    "title": "Retries in Pulse",
    "body_markdown": _BODY,
    "meta_description": (
        "How retries work in Pulse, why a retry never double-posts, and what "
        "the backoff actually does when a platform is down."
    ),
    "keywords": ["retries"],
    "focus_keyword": "retries",
    "slug": "retries-in-pulse",
    "cover_image_url": None,
}


# --------------------------------------------------------------------------- #
# The arithmetic that made the hole                                            #
# --------------------------------------------------------------------------- #


def test_a_piece_with_no_meta_description_scores_at_the_threshold_not_below_it():
    """The bug in one number.

    Not an assertion that 70 is the *right* score — it is a pin on the fact that
    the score alone cannot be trusted to catch this, so that re-tuning the
    deductions later does not make the gate below look redundant.
    """
    score = seo.seo_score(**{**_CLEAN, "meta_description": ""})

    assert score == 70
    assert score >= seo.SEO_SCORE_THRESHOLD


def test_a_piece_with_no_title_also_clears_the_score_threshold():
    assert seo.seo_score(**{**_CLEAN, "title": ""}) >= seo.SEO_SCORE_THRESHOLD


def test_the_audit_calls_both_of_those_errors_while_the_score_waves_them_through():
    """The two halves disagreeing, which is what ``blocking_issues`` reads."""
    for field, broken in (("meta_description", ""), ("title", "")):
        issues = seo.audit(**{**_CLEAN, field: broken})
        assert [i.level for i in issues if i.level == "error"] == ["error"], field


# --------------------------------------------------------------------------- #
# blocking_issues                                                              #
# --------------------------------------------------------------------------- #


def test_a_clean_piece_has_no_blocking_issues():
    assert seo.blocking_issues(**_CLEAN) == []


def test_blocking_issues_names_the_missing_meta_description():
    issues = seo.blocking_issues(**{**_CLEAN, "meta_description": ""})

    assert [i.field for i in issues] == ["meta_description"]
    assert "meta description" in issues[0].message.lower()


def test_blocking_issues_names_the_missing_title():
    assert [i.field for i in seo.blocking_issues(**{**_CLEAN, "title": ""})] == ["title"]


def test_blocking_issues_ignores_warnings():
    """A thin, keyword-stuffed, headingless post is weak, not broken.

    The score is what holds that back. Promoting warnings here would make the
    review queue the only outcome, which is the feature switched off.
    """
    weak = {
        **_CLEAN,
        "body_markdown": "Retries retries retries retries retries.",
        "cover_image_url": None,
        "slug": "x" * 80,
    }

    assert seo.audit(**weak), "expected this piece to have issues at all"
    assert seo.blocking_issues(**weak) == []


def test_both_errors_are_reported_together():
    issues = seo.blocking_issues(**{**_CLEAN, "title": "", "meta_description": ""})

    assert sorted(i.field for i in issues) == ["meta_description", "title"]


# --------------------------------------------------------------------------- #
# The gate, through the pipeline                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def auto_project(db, project, connect, monkeypatch):
    """A project that would auto-publish anything it is handed.

    Connected, so ``publishable_destinations`` does not demote the piece for a
    reason none of these tests are about; and the publish itself is stubbed,
    because what is asserted is the routing decision, not the HTTP call after it.
    """
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    db.commit()
    connect("devto")
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])
    return project


def _generated(**overrides) -> GeneratedContent:
    base = {
        "title": _CLEAN["title"],
        "body_markdown": _CLEAN["body_markdown"],
        "excerpt": "How retries work.",
        "meta_description": _CLEAN["meta_description"],
        "keywords": ["retries"],
        "tags": ["python"],
        "focus_keyword": "retries",
        # Comfortably over `autopilot_auto_publish_confidence`, so confidence is
        # never what decides these.
        "confidence": 0.99,
    }
    base.update(overrides)
    return GeneratedContent(**base)


@pytest.fixture
def writes(monkeypatch):
    """Let each test choose what the model 'returns'."""

    def _install(generated: GeneratedContent) -> None:
        monkeypatch.setattr(
            content_generator, "generate", lambda *a, **k: generated
        )

    return _install


def _route(db, project):
    return content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )


def test_a_clean_piece_still_auto_publishes(db, auto_project, writes):
    """The control. Without this the tests below pass for the wrong reason."""
    writes(_generated())

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.seo_errors == []


def test_a_piece_with_no_meta_description_goes_to_review(
    db, auto_project, writes
):
    """The bug. Pulse published these under the user's name, unreviewed."""
    writes(_generated(meta_description=""))

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW
    # And specifically *not* because it scored badly — it scored the threshold.
    assert routed.seo_score >= seo.SEO_SCORE_THRESHOLD


def test_the_reason_reaches_the_reviewer(db, auto_project, writes):
    """A held-back piece whose reason is only in the logs is a mystery in a queue."""
    writes(_generated(meta_description=""))

    routed = _route(db, auto_project)

    assert routed.seo_errors
    assert "meta description" in routed.seo_errors[0].lower()
    assert routed.content.source["seo_errors"] == routed.seo_errors


def test_the_reason_reaches_the_task_summary(db, auto_project, writes):
    writes(_generated(meta_description=""))

    summary = _route(db, auto_project).summary()

    assert summary["seo_errors"]
    assert summary["status"] == content_pipeline.QUEUED_FOR_REVIEW


def test_a_clean_summary_carries_no_seo_errors_key(
    db, auto_project, writes
):
    """Same contract as ``dead_links``: absent rather than empty."""
    writes(_generated())

    assert "seo_errors" not in _route(db, auto_project).summary()


def test_the_errors_are_banked_even_when_something_else_held_the_piece(
    db, project, writes
):
    """Asked regardless of ``auto``, so a reviewer sees every reason at once.

    Autopilot is off here, so the piece was never going to publish. The SEO
    errors on it are still true and still what the editor has to fix.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    writes(_generated(meta_description=""))

    routed = _route(db, project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.seo_errors
