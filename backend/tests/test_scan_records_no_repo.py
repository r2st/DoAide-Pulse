"""A project with no repo still records that it was looked at.

Before this fix, ``scan_project`` returned ``no_repo`` without calling
``record_scan``, so ``last_scanned_at`` stayed NULL forever and the project
appeared as "never scanned" in every health check.
"""
from __future__ import annotations

import pytest

from app.models.project import AutopilotMode, Project, Tone
from app.tasks import autopilot_tasks


def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", _no_close(db))


def _no_repo_project(db, user, *, mode=AutopilotMode.FULL):
    row = Project(
        user_id=user.id,
        name="No Repo Project",
        slug="no-repo-project",
        description="A project with no GitHub repo.",
        repo_url=None,
        tone=Tone.TECHNICAL,
        autopilot_mode=mode,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_scan_project_records_scan_for_no_repo(db, user):
    """A project with no repo must still get last_scanned_at stamped."""
    project = _no_repo_project(db, user)
    assert project.last_scanned_at is None
    assert project.scan_count == 0

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "no_repo"
    db.refresh(project)
    assert project.last_scanned_at is not None
    assert project.scan_count == 1


def test_scan_project_records_scan_for_non_github_url(db, user):
    """A GitLab URL is not scannable — but the project is still marked scanned."""
    row = Project(
        user_id=user.id,
        name="GitLab Project",
        slug="gitlab-project",
        repo_url="https://gitlab.com/owner/repo",
        tone=Tone.TECHNICAL,
        autopilot_mode=AutopilotMode.FULL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    assert row.last_scanned_at is None

    result = autopilot_tasks.scan_project(row.id)

    assert result["status"] == "no_repo"
    db.refresh(row)
    assert row.last_scanned_at is not None


def test_scan_all_still_warns_about_stranded_projects(db, user, monkeypatch, caplog):
    """The stranded warning must still fire so operators know the config is wrong."""
    _no_repo_project(db, user)

    monkeypatch.setattr(autopilot_tasks, "SessionLocal", _no_close(db))

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "no repo and no trigger" in caplog.text


def test_second_scan_respects_interval(db, user, monkeypatch):
    """Once last_scanned_at is set, the interval should apply."""
    project = _no_repo_project(db, user)
    project.autopilot_min_interval_hours = 24
    db.commit()

    autopilot_tasks.scan_project(project.id)
    db.refresh(project)
    first_scan = project.last_scanned_at
    assert first_scan is not None

    result = autopilot_tasks.scan_project(project.id)
    db.refresh(project)
    assert result["status"] == "no_repo"
    assert project.scan_count == 2
