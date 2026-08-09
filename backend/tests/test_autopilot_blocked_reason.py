"""A project armed for autopilot that can never fire says so.

``scan_all_projects`` selects on ``repo_url IS NOT NULL``, so a project set to
draft or auto without one is not skipped with a reason — it is simply not in the
result set. The switch reads on, the badge reads auto, and the only symptom is
that nothing is ever written. These assert the project can be asked directly.
"""
from __future__ import annotations

import pytest

from app.models.project import AutopilotMode, Project
from app.models.trigger import Trigger, TriggerKind


@pytest.fixture
def repoless(db, user) -> Project:
    row = Project(
        user_id=user.id,
        name="Ambalika Bhowmik Interior Design",
        slug="ambalika",
        description="A portfolio site.",
        autopilot_mode=AutopilotMode.AUTO,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_autopilot_on_with_no_repo_and_no_trigger_is_blocked(repoless):
    assert repoless.autopilot_blocked_reason == (
        "No repository is linked and no trigger is set, so nothing can start a "
        "piece."
    )


def test_autopilot_off_is_not_blocked(db, repoless):
    """Off is a choice, not a fault — there is nothing to report."""
    repoless.autopilot_mode = AutopilotMode.OFF
    db.commit()

    assert repoless.autopilot_blocked_reason is None


def test_a_trigger_is_reason_enough_to_have_no_repo(db, repoless):
    """An RSS or schedule trigger drives the same pipeline without a repo."""
    db.add(
        Trigger(
            project_id=repoless.id,
            kind=TriggerKind.SCHEDULE,
            name="Every Friday",
            is_active=True,
            config={},
            state={},
        )
    )
    db.commit()
    db.refresh(repoless)

    assert repoless.autopilot_blocked_reason is None


def test_an_inactive_trigger_does_not_count(db, repoless):
    db.add(
        Trigger(
            project_id=repoless.id,
            kind=TriggerKind.SCHEDULE,
            name="Paused",
            is_active=False,
            config={},
            state={},
        )
    )
    db.commit()
    db.refresh(repoless)

    assert repoless.autopilot_blocked_reason is not None


def test_a_repo_that_is_not_github_is_blocked(db, repoless):
    """The scan speaks one API — a GitLab URL passes the NOT NULL and then stalls."""
    repoless.repo_url = "https://gitlab.com/r2st/thing"
    db.commit()

    assert "GitHub" in (repoless.autopilot_blocked_reason or "")


def test_a_working_project_reports_nothing(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    db.commit()

    assert project.autopilot_blocked_reason is None


def test_the_api_reports_the_reason(client, auth, repoless):
    resp = client.get("/api/v1/projects", headers=auth)

    assert resp.status_code == 200, resp.text
    blocked = {p["name"]: p["autopilot_blocked_reason"] for p in resp.json()}
    assert blocked["Ambalika Bhowmik Interior Design"] is not None
