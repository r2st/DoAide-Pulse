"""A project armed for autopilot that can never fire says so.

``scan_all_projects`` selects on ``repo_url IS NOT NULL``, so a project set to
draft or auto without one is not skipped with a reason — it is simply not in the
result set. The switch reads on, the badge reads auto, and the only symptom is
that nothing is ever written. These assert the project can be asked directly.

There are two ways to never fire, and the property reports both. A project that
cannot *start* a piece has no repo the scan can read and no trigger. A project
that cannot *finish* one has nowhere to publish it: every destination it names
is unconnected, unimplemented, or there are none. The second half is the shape
this project actually shipped — an autopilot destination the account had never
connected, which wrote pieces and parked every one of them in review — and the
badge whose whole job is to explain a silent autopilot reported nothing wrong.
"""
from __future__ import annotations

import pytest

from app.models.project import AutopilotMode, Project
from app.models.publication import Platform
from app.models.trigger import Trigger, TriggerKind
from app.services import publishers


@pytest.fixture
def repoless(db, user, connect) -> Project:
    """No repo and no trigger, but somewhere to publish to.

    The destination matters even though these tests are about *starting*: with
    nothing connected the project is blocked twice over, and every assertion
    about the start half would pass on the finish half's message instead.
    """
    connect(Platform.DEVTO)
    row = Project(
        user_id=user.id,
        name="Ambalika Bhowmik Interior Design",
        slug="ambalika",
        description="A portfolio site.",
        autopilot_mode=AutopilotMode.AUTO,
        autopilot_platforms=[Platform.DEVTO.value],
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


def test_a_working_project_reports_nothing(db, project, connect):
    """A repo the scan can read, and a destination the account is connected to."""
    connect(Platform.DEVTO)
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.DEVTO.value]
    db.commit()

    assert project.autopilot_blocked_reason is None


def test_the_api_reports_the_reason(client, auth, repoless):
    resp = client.get("/api/v1/projects", headers=auth)

    assert resp.status_code == 200, resp.text
    blocked = {p["name"]: p["autopilot_blocked_reason"] for p in resp.json()}
    assert blocked["Ambalika Bhowmik Interior Design"] is not None


# --------------------------------------------------------------------------- #
# Nowhere to publish: the piece is written and then goes nowhere               #
# --------------------------------------------------------------------------- #


def test_an_unconnected_destination_blocks_and_is_named(db, project):
    """The production failure. Nothing is connected, so nothing publishes.

    ``publishable_destinations`` drops the destination and ``generate_and_route``
    parks the piece in review — which from the outside is the quality gates
    working. The project has to be the thing that says otherwise.
    """
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.BLUESKY.value, Platform.DEVTO.value]
    db.commit()

    reason = project.autopilot_blocked_reason or ""
    assert "not connected" in reason
    assert "bluesky" in reason and "devto" in reason


def test_one_connected_destination_is_enough(db, project, connect):
    """Blocked means *nothing* can go out, not that something was dropped."""
    connect(Platform.DEVTO)
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.BLUESKY.value, Platform.DEVTO.value]
    db.commit()

    assert project.autopilot_blocked_reason is None


def test_auto_with_no_destinations_at_all_is_blocked(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = []
    db.commit()

    assert "names no platforms" in (project.autopilot_blocked_reason or "")


def test_a_destination_with_no_finished_adapter_is_blocked(db, project, connect):
    """Connected is not the same as publishable — the registry has the last word."""
    unfinished = next(
        p
        for p in Platform
        if not publishers.get_adapter(p).implemented
    )
    connect(unfinished)
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [unfinished.value]
    db.commit()

    reason = project.autopilot_blocked_reason or ""
    assert "None of the platforms" in reason


def test_draft_mode_with_nowhere_to_publish_is_not_blocked(db, project):
    """On ``draft`` the pieces are meant to stop for a human. That is the setting."""
    project.autopilot_mode = AutopilotMode.DRAFT
    project.autopilot_platforms = []
    db.commit()

    assert project.autopilot_blocked_reason is None


def test_the_start_reason_wins_when_a_project_is_blocked_both_ways(db, user):
    """One badge, and "nothing writes a piece" comes before "it goes nowhere"."""
    row = Project(
        user_id=user.id,
        name="Doubly stuck",
        slug="doubly-stuck",
        description="No repo, no trigger, no destination.",
        autopilot_mode=AutopilotMode.AUTO,
        autopilot_platforms=[],
    )
    db.add(row)
    db.commit()

    assert row.autopilot_blocked_reason == (
        "No repository is linked and no trigger is set, so nothing can start a "
        "piece."
    )


def test_the_reported_project_is_the_one_that_stalled_in_production(db, project):
    """End to end over the property: auto + a destination nobody connected.

    The piece is written, every destination is dropped, and it lands in review.
    Asserted together because the badge's claim is about that whole sequence,
    not about the column it reads.
    """
    from app.services import content_pipeline

    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = [Platform.BLUESKY.value]
    db.commit()

    assert content_pipeline.publishable_destinations(project).usable == []
    assert project.autopilot_blocked_reason is not None
