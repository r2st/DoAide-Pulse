"""The autopilot's edges: a repo it cannot read, an idea table that grows, a
broker that is not there.

These three have nothing in common except that none of them is the happy path,
and each fails in a way that is invisible until it is expensive. An unreachable
repo that moved the watermark would skip real commits. An idea table with no cap
grows by two rows per project per scan forever. A dispatch loop that lets a
broker error escape leaves the rest of the fleet unscanned.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.models.content import ContentIdea, ContentType
from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Project
from app.services import github_client
from app.tasks import autopilot_tasks

# --------------------------------------------------------------------------- #
# A repo the scan cannot read                                                  #
# --------------------------------------------------------------------------- #


def test_a_repo_that_cannot_be_read_records_the_attempt_but_not_a_watermark(
    db, project, stub_github, monkeypatch, caplog
):
    """A renamed or deleted repo, or GitHub having a bad afternoon.

    ``last_scanned_at`` moves because we did try; the commit watermark does not,
    because we learned nothing about where the repo is. Moving it here would
    silently skip every commit made while the repo was unreachable.
    """
    project.last_seen_commit_sha = "abc123"
    db.commit()

    def unreachable(*args, **kwargs):
        raise github_client.GitHubError("404 Not Found — repository may have moved")

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", unreachable)

    with caplog.at_level("WARNING"):
        result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "unreachable"
    db.refresh(project)
    assert project.last_seen_commit_sha == "abc123"
    assert project.last_scanned_at is not None
    assert "could not read" in caplog.text


def test_a_rate_limit_does_not_even_record_the_attempt(
    db, project, stub_github, monkeypatch
):
    """The distinction from ``unreachable``, which does set ``last_scanned_at``.

    Rate-limited means the request never reached the repo, so there is no
    attempt to record — and recording one would make the next sweep's "when did
    we last look" answer a time at which we did not look.
    """
    monkeypatch.setattr(
        autopilot_tasks.github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(
            github_client.GitHubRateLimited("rate limit exhausted")
        ),
    )

    assert autopilot_tasks.scan_project(project.id)["status"] == "rate_limited"
    db.refresh(project)
    assert project.last_scanned_at is None


def test_a_secondary_rate_limit_is_a_rate_limit_and_not_an_unreachable_repo(
    db, project, task_session, monkeypatch
):
    """End to end from the HTTP response, because the mis-read was in the parse.

    GitHub reports its secondary limit as a 403 with ``Retry-After`` and the
    request quota barely touched — which this module's error classifier read as
    "private repo, no token", i.e. a plain ``GitHubError``. The autopilot
    answers that by stamping ``last_scanned_at`` and calling the project
    unreachable. The secondary limit is per *account*, so a single burst did
    that to every project in the sweep, and every one of them then reported a
    last-looked time at which nothing had been looked at.
    """
    import httpx

    project.last_seen_commit_sha = "abc123"
    db.commit()

    def throttled(url, **kwargs):
        return httpx.Response(
            status_code=403,
            headers={"Retry-After": "60", "X-RateLimit-Remaining": "4873"},
            json={"message": "You have exceeded a secondary rate limit."},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(github_client.httpx, "get", throttled)
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", task_session)

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "rate_limited"
    db.refresh(project)
    assert project.last_scanned_at is None
    assert project.last_seen_commit_sha == "abc123"


def test_a_scan_that_runs_out_of_time_reports_the_timeout(
    db, project, stub_github, monkeypatch, caplog
):
    """The soft limit is a signal to stop, not an error to retry into."""
    monkeypatch.setattr(
        autopilot_tasks.github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    with caplog.at_level("WARNING"):
        result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "timeout"
    assert "timed out" in caplog.text


def test_an_unexpected_crash_is_reported_rather_than_raised(
    db, project, stub_github, monkeypatch, caplog
):
    """``scan_project`` is documented as never raising; one project must not
    take down a worker that has the rest of the fleet queued behind it."""
    monkeypatch.setattr(
        autopilot_tasks.github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("nobody predicted this")),
    )

    with caplog.at_level("ERROR"):
        result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "error"
    assert "nobody predicted this" in result["error"]
    assert "crashed" in caplog.text


def test_a_project_whose_owner_was_deactivated_is_not_scanned(
    db, project, user, stub_github
):
    """Deactivating an account has to stop what it already had running."""
    user.is_active = False
    db.commit()

    assert autopilot_tasks.scan_project(project.id)["status"] == "skipped"
    assert stub_github["calls"] == []


# --------------------------------------------------------------------------- #
# The idea cap                                                                 #
# --------------------------------------------------------------------------- #


def _bank(db, project, count, *, used=False, start_days_ago=100):
    """*count* unused ideas, oldest first."""
    rows = [
        ContentIdea(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            headline=f"Idea {i}",
            rationale="r",
            used_content_id=1 if used else None,
            created_at=utcnow() - timedelta(days=start_days_ago - i),
        )
        for i in range(count)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def test_a_project_inside_the_cap_loses_nothing(db, project):
    _bank(db, project, settings.autopilot_ideas_cap - 1)

    assert autopilot_tasks._prune_ideas(db, project.id) == 0
    assert db.query(ContentIdea).count() == settings.autopilot_ideas_cap - 1


def test_a_project_exactly_at_the_cap_loses_nothing(db, project):
    """The cap is a ceiling, not a threshold to prune past."""
    _bank(db, project, settings.autopilot_ideas_cap)

    assert autopilot_tasks._prune_ideas(db, project.id) == 0


def test_the_oldest_ideas_are_the_ones_dropped(db, project):
    over = 3
    _bank(db, project, settings.autopilot_ideas_cap + over)

    pruned = autopilot_tasks._prune_ideas(db, project.id)
    db.commit()

    assert pruned == over
    remaining = {row.headline for row in db.query(ContentIdea).all()}
    # Idea 0 is the oldest of the batch, so it goes first.
    assert "Idea 0" not in remaining
    assert f"Idea {settings.autopilot_ideas_cap + over - 1}" in remaining
    assert len(remaining) == settings.autopilot_ideas_cap


def test_ideas_already_written_from_do_not_count_against_the_cap(db, project):
    """A used idea is a record of what was written, not a pending suggestion."""
    _bank(db, project, settings.autopilot_ideas_cap * 2, used=True)
    _bank(db, project, 1, start_days_ago=5)

    assert autopilot_tasks._prune_ideas(db, project.id) == 0
    assert db.query(ContentIdea).count() == settings.autopilot_ideas_cap * 2 + 1


def test_pruning_is_scoped_to_one_project(db, project, user):
    other = Project(user_id=user.id, name="Other", slug="other")
    db.add(other)
    db.commit()
    _bank(db, other, settings.autopilot_ideas_cap + 5)

    assert autopilot_tasks._prune_ideas(db, project.id) == 0
    assert (
        db.query(ContentIdea).filter_by(project_id=other.id).count()
        == settings.autopilot_ideas_cap + 5
    )


def test_a_project_with_no_ideas_at_all_prunes_nothing(db, project):
    assert autopilot_tasks._prune_ideas(db, project.id) == 0


# --------------------------------------------------------------------------- #
# Fanning the fleet out                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_projects(db, user):
    rows = [
        Project(
            user_id=user.id,
            name=f"P{i}",
            slug=f"p{i}",
            repo_url=f"https://github.com/r2st/p{i}",
            autopilot_mode=AutopilotMode.DRAFT,
        )
        for i in range(2)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def test_a_broker_that_is_not_there_scans_inline_instead_of_giving_up(
    db, two_projects, stub_github, monkeypatch
):
    """Losing the whole sweep because Redis is down is worse than running slow."""
    inline: list[int] = []

    def no_broker(project_id):
        raise OSError("connection refused")

    # ``scan_all_projects`` reaches for ``scan_project.delay`` and then falls
    # back to calling ``scan_project`` itself, so the stub has to answer to both
    # names — one refusing, one recording.
    monkeypatch.setattr(
        autopilot_tasks, "scan_project", lambda pid: inline.append(pid)
    )
    autopilot_tasks.scan_project.delay = no_broker

    result = autopilot_tasks.scan_all_projects()

    assert result["scanned"] == 2
    assert result["dispatched"] == 2
    assert sorted(inline) == sorted(p.id for p in two_projects)


def test_a_dispatch_that_runs_out_of_time_stops_rather_than_scanning_inline(
    db, two_projects, stub_github, monkeypatch, caplog
):
    """A soft timeout must not be read as a dead broker.

    The inline fallback scans a repo and calls a model. Doing that on a task
    with seconds left before its hard limit is how a timeout becomes a killed
    worker; the undispatched projects are picked up by the next sweep instead.
    """
    inline: list[int] = []

    def times_out(project_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        autopilot_tasks, "scan_project", lambda pid: inline.append(pid)
    )
    autopilot_tasks.scan_project.delay = times_out

    with caplog.at_level("WARNING"):
        result = autopilot_tasks.scan_all_projects()

    assert result["dispatched"] == 0
    assert inline == [], "the fallback must not run after a soft timeout"
    assert "dispatch timed out" in caplog.text


def test_a_project_with_the_autopilot_on_and_nowhere_to_scan_is_named_in_the_logs(
    db, user, stub_github, caplog
):
    """Until this warning, the only evidence was that it never produced anything."""
    stranded = Project(
        user_id=user.id,
        name="Stranded",
        slug="stranded",
        repo_url=None,
        autopilot_mode=AutopilotMode.DRAFT,
    )
    db.add(stranded)
    db.commit()

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "can never scan" in caplog.text
    assert "Stranded" in caplog.text


def test_a_paused_project_with_no_repo_is_not_reported_as_stranded(
    db, user, stub_github, caplog
):
    """Autopilot off means it is not waiting to scan — nothing to warn about."""
    db.add(
        Project(
            user_id=user.id,
            name="Dormant",
            slug="dormant",
            repo_url=None,
            autopilot_mode=AutopilotMode.OFF,
        )
    )
    db.commit()

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "Dormant" not in caplog.text
