"""The repo monitor and the decisions it makes.

GitHub is stubbed out with fabricated activity, so what is under test is the
policy — when Herald writes, when it stays quiet, and what it does with the
watermark — rather than the HTTP client.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus
from app.models.project import AutopilotMode, Project
from app.services import github_client
from app.services.github_client import Commit, RepoActivity
from app.tasks import autopilot_tasks


def make_activity(*, commits: int = 0, release: bool = False, head: str = "abc123"):
    return RepoActivity(
        full_name="r2st/Herald",
        new_commits=[
            Commit(
                sha=f"sha{i}",
                message=f"feat: thing {i}\n\nbody",
                author="r2st",
                committed_at=datetime(2026, 7, 1, tzinfo=UTC),
                url="https://github.com/r2st/Herald/commit/x",
            )
            for i in range(commits)
        ],
        new_release=(
            github_client.Release(
                tag="v1.2.0",
                name="Calendar drag-and-drop",
                body="- Drag to reschedule\n- SEO panel",
                published_at=datetime(2026, 7, 20, tzinfo=UTC),
                url="https://github.com/r2st/Herald/releases/v1.2.0",
                prerelease=False,
            )
            if release
            else None
        ),
        head_sha=head,
        latest_tag="v1.2.0" if release else None,
    )


@pytest.fixture
def stub_github(monkeypatch):
    """Replace the GitHub fetch with a fixture, and record what it was asked."""
    calls: list[dict] = []
    activity = {"value": make_activity()}

    def fake_fetch(full_name, *, since_sha=None, since_tag=None):
        calls.append({"full_name": full_name, "since_sha": since_sha})
        return activity["value"]

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", fake_fetch)
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", _session_factory())
    return {"calls": calls, "set": lambda a: activity.update(value=a)}


_SESSION_HOLDER: dict = {}


def _session_factory():
    """Hand the task the test's session, without it closing the shared one."""

    def factory():
        session = _SESSION_HOLDER["session"]

        class NoCloseProxy:
            def __getattr__(self, name):
                return getattr(session, name)

            def close(self):
                # The fixture owns this session's lifetime, not the task.
                pass

        return NoCloseProxy()

    return factory


@pytest.fixture(autouse=True)
def _share_session(db):
    _SESSION_HOLDER["session"] = db
    yield
    _SESSION_HOLDER.clear()


def test_first_scan_only_baselines(db, project, stub_github):
    stub_github["set"](make_activity(commits=50, release=True))
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "baselined"
    assert db.query(Content).count() == 0
    # The watermark moved, so the next scan starts from here.
    db.refresh(project)
    assert project.last_seen_commit_sha == "abc123"


def test_a_handful_of_commits_is_below_the_threshold(db, project, stub_github):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=2))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "below_threshold"
    assert db.query(Content).count() == 0


def test_a_release_always_warrants_a_post(db, project, stub_github):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    content = db.query(Content).one()
    # A release becomes an announcement, and it waits for a human.
    assert content.content_type.value == "announcement"
    assert content.status == ContentStatus.REVIEW
    assert content.source["trigger"] == "release"
    assert content.source["release_tag"] == "v1.2.0"


def test_autopilot_off_banks_ideas_but_writes_nothing(db, project, stub_github):
    project.autopilot_mode = AutopilotMode.OFF
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=30, release=True))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "ideas_only"
    assert db.query(Content).count() == 0
    from app.models.content import ContentIdea

    assert db.query(ContentIdea).count() > 0


def test_auto_mode_still_reviews_a_low_confidence_draft(db, project, stub_github):
    """No provider is configured, so generation falls back at confidence 0.0."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "queued_for_review"
    assert db.query(Content).one().status == ContentStatus.REVIEW


def test_daily_limit_stops_a_busy_repo(db, project, stub_github, monkeypatch):
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=1, release=True))
    monkeypatch.setattr(settings, "autopilot_daily_content_limit", 1)

    assert autopilot_tasks.scan_project(project.id)["status"] == "queued_for_review"

    project.last_seen_commit_sha = "older"
    db.commit()
    assert autopilot_tasks.scan_project(project.id)["status"] == "daily_limit_reached"


def test_rate_limit_leaves_the_watermark_alone(db, project, stub_github, monkeypatch):
    project.last_seen_commit_sha = "keep-me"
    db.commit()

    def raise_rate_limit(*args, **kwargs):
        raise github_client.GitHubRateLimited("slow down")

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", raise_rate_limit)

    assert autopilot_tasks.scan_project(project.id)["status"] == "rate_limited"
    db.refresh(project)
    # We did not actually look, so we must not claim to have seen anything.
    assert project.last_seen_commit_sha == "keep-me"


def test_project_without_a_repo_is_skipped(db, user, stub_github):
    project = Project(user_id=user.id, name="No repo", slug="no-repo")
    db.add(project)
    db.commit()
    assert autopilot_tasks.scan_project(project.id)["status"] == "no_repo"


def test_scanning_a_missing_project_does_not_raise(db, stub_github):
    assert autopilot_tasks.scan_project(9999)["status"] == "skipped"
