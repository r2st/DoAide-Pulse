"""Tests for the FULL autopilot mode.

FULL mode publishes unconditionally — no confidence threshold — so a piece
that would sit in review under AUTO goes straight out under FULL.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus
from app.models.project import AutopilotMode, Project
from app.services import content_pipeline
from app.tasks import autopilot_tasks

from .conftest import repo_activity as make_activity


@pytest.fixture(autouse=True)
def _connected(connect):
    connect("devto")


def _low_confidence_generation(monkeypatch):
    from app.services.content_generator import GeneratedContent

    monkeypatch.setattr(
        autopilot_tasks.content_generator,
        "generate",
        lambda project, content_type, **kw: GeneratedContent(
            title="Pulse 1.2.0 is out",
            body_markdown="## What changed\n\n" + ("Real prose. " * 200),
            excerpt="Pulse 1.2.0 is out.",
            meta_description="Pulse 1.2.0 is out, with a faster publish sweep.",
            keywords=["pulse"],
            tags=["python"],
            confidence=0.2,
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
        ),
    )


def _bypass_quality_gates(monkeypatch):
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])
    monkeypatch.setattr(content_pipeline.seo, "seo_score", lambda **kw: 100)
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)


def test_full_mode_is_a_valid_enum_value():
    assert AutopilotMode("full") == AutopilotMode.FULL
    assert AutopilotMode.FULL.value == "full"


def test_full_mode_round_trips_through_database(db, user):
    p = Project(
        user_id=user.id,
        name="Full Test",
        slug="full-test",
        repo_url="https://github.com/r2st/test",
        autopilot_mode=AutopilotMode.FULL,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    assert p.autopilot_mode == AutopilotMode.FULL


def test_full_mode_publishes_despite_low_confidence(
    db, project, stub_github, monkeypatch
):
    project.autopilot_mode = AutopilotMode.FULL
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _low_confidence_generation(monkeypatch)
    _bypass_quality_gates(monkeypatch)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "auto_published"
    content = db.query(Content).one()
    assert content.status == ContentStatus.APPROVED


def test_auto_mode_reviews_low_confidence(db, project, stub_github, monkeypatch):
    """Contrast: AUTO mode with the same low confidence sends to review."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    _low_confidence_generation(monkeypatch)
    _bypass_quality_gates(monkeypatch)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"


def test_full_mode_blocked_reason_checks_destinations(db, project):
    project.autopilot_mode = AutopilotMode.FULL
    project.autopilot_platforms = ["devto"]
    db.commit()
    db.refresh(project)
    reason = project.autopilot_blocked_reason
    assert reason is None or "destination" not in reason.lower()


def test_list_projects_with_full_mode(client, auth, db, user):
    p = Project(
        user_id=user.id,
        name="Full Project",
        slug="full-project",
        autopilot_mode=AutopilotMode.FULL,
    )
    db.add(p)
    db.commit()
    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200
    by_slug = {p["slug"]: p for p in resp.json()}
    assert by_slug["full-project"]["autopilot_mode"] == "full"
