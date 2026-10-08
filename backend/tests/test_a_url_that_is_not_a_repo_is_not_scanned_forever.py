"""A ``repo_url`` that is not a GitHub repo, which is not the same as none.

The autopilot sweep used to ask one question about a project's repo — ``repo_url
IS NOT NULL`` — and treat the answer as "can this be scanned". A GitLab URL
passes that. So does the repo's web page with a trailing ``/issues``, and so
does a typo. Every one of those projects was selected, dispatched to a worker,
and turned away by :func:`app.tasks.autopilot_tasks.scan_project` at
``repo_full_name`` with ``no_repo``.

That return is correct and it deliberately does not record a scan — nothing
looked at GitHub, and timing it would mix a sub-millisecond no-op into an
average kept to show when the *network* half got slow. But it left
``last_scanned_at`` NULL, and a project that has never scanned is always due,
whatever its interval. So the sweep dispatched it again on the next tick, and
the tick after that, for as long as the project existed: an hour's cadence of
work that could not possibly succeed, with ``autopilot_min_interval_hours`` set
and never once applying.

The warning that exists to name a project which can never scan did not name it
either, because that query asked for ``repo_url IS NULL`` — the other half of
the same fault. This project was the worse half to leave silent, because it is
the one that *looks* configured: there is a URL in the box.

:meth:`app.models.project.Project._cannot_start` had always stated the rule as
one thing. These tests pin the sweep to that same rule.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.mixins import utcnow
from app.models.project import (
    AutopilotMode,
    Project,
    Tone,
    repo_full_name,
)
from app.models.trigger import Trigger, TriggerKind
from app.tasks import autopilot_tasks

# --------------------------------------------------------------------------- #
# repo_full_name, which is the whole of the rule                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "https://gitlab.com/acme/thing",
        "https://bitbucket.org/acme/thing",
        "https://example.com/acme/thing",
        # The repo's web page rather than the repo. A real paste, and the one
        # that reads most like a working setting.
        "https://github.com/r2st/DoAide-Pulse/issues",
        "https://github.com/r2st",
        "not a url at all",
    ],
)
def test_a_url_the_scan_cannot_read_has_no_full_name(url):
    assert repo_full_name(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/r2st/DoAide-Pulse",
        "https://github.com/r2st/DoAide-Pulse/",
        "https://github.com/r2st/DoAide-Pulse.git",
        "http://github.com/r2st/DoAide-Pulse",
        "git@github.com:r2st/DoAide-Pulse.git",
        "  https://github.com/r2st/DoAide-Pulse  ",
        "https://GitHub.com/r2st/DoAide-Pulse",
    ],
)
def test_the_github_url_shapes_people_actually_paste_all_parse(url):
    assert repo_full_name(url) == "r2st/DoAide-Pulse"


def test_the_property_and_the_function_cannot_drift(db, user):
    """The sweep asks the function and ``scan_project`` asks the property.

    They are the same question, and a project dispatched because one said yes
    and turned away because the other said no is exactly the loop these tests
    exist to close.
    """
    row = _project(db, user, "gitlab", repo_url="https://gitlab.com/acme/thing")
    assert row.repo_full_name is repo_full_name(row.repo_url) is None

    row.repo_url = "https://github.com/r2st/DoAide-Pulse"
    assert row.repo_full_name == repo_full_name(row.repo_url) == "r2st/DoAide-Pulse"


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #


def _project(
    db,
    user,
    slug,
    *,
    repo_url,
    mode=AutopilotMode.AUTO,
    interval=24,
    last_scanned_at=None,
):
    row = Project(
        user_id=user.id,
        name=slug,
        slug=slug,
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
        repo_url=repo_url,
        autopilot_mode=mode,
        autopilot_min_interval_hours=interval,
        last_scanned_at=last_scanned_at,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def dispatched(monkeypatch, db):
    """Record which project ids the sweep hands to a worker."""
    calls: list[int] = []

    class _Recorder:
        def delay(self, project_id):
            calls.append(project_id)

        def __call__(self, project_id):  # the inline-fallback path
            calls.append(project_id)
            return {"status": "no_news"}

    monkeypatch.setattr(autopilot_tasks, "scan_project", _Recorder())
    return calls


def test_a_url_the_scan_cannot_read_is_not_dispatched(
    db, user, dispatched, stub_github
):
    _project(db, user, "gitlab", repo_url="https://gitlab.com/acme/thing")

    result = autopilot_tasks.scan_all_projects()

    assert dispatched == []
    assert result["scanned"] == 0


def test_it_is_still_not_dispatched_on_the_next_sweep(db, user, dispatched, stub_github):
    """The regression this file is named for.

    One skipped dispatch is not the point — the point is that it stays skipped.
    Before the fix the second sweep dispatched it again, and so did the
    hundredth, because the only thing that would have held it back is a
    ``last_scanned_at`` the ``no_repo`` return never writes.
    """
    _project(db, user, "gitlab", repo_url="https://gitlab.com/acme/thing")

    for _ in range(3):
        autopilot_tasks.scan_all_projects()

    assert dispatched == []


def test_a_scannable_project_beside_it_is_unaffected(db, user, dispatched, stub_github):
    """The filter must drop one row, not shorten the sweep."""
    _project(db, user, "gitlab", repo_url="https://gitlab.com/acme/thing")
    good = _project(db, user, "pulse", repo_url="https://github.com/r2st/DoAide-Pulse")

    autopilot_tasks.scan_all_projects()

    assert dispatched == [good.id]


def test_it_is_named_in_the_warning_that_exists_to_name_it(
    db, user, dispatched, stub_github, caplog
):
    _project(db, user, "Gitlab", repo_url="https://gitlab.com/acme/thing")

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "can never scan" in caplog.text
    assert "Gitlab" in caplog.text


def test_a_project_with_no_repo_at_all_is_still_named(
    db, user, dispatched, stub_github, caplog
):
    """The half that was already reported keeps being reported.

    The two queries became one; this is the older behaviour pinned so the
    merge cannot quietly drop it.
    """
    _project(db, user, "Repoless", repo_url=None)

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "Repoless" in caplog.text


def test_an_active_trigger_is_reason_enough_for_an_unscannable_url(
    db, user, dispatched, stub_github, caplog
):
    """A trigger drives the project through the same pipeline without the scan.

    So the project is not stranded — but it is still not worth dispatching,
    because ``scan_project`` would turn it away for the same reason as ever.
    """
    row = _project(db, user, "Gitlab", repo_url="https://gitlab.com/acme/thing")
    db.add(
        Trigger(
            project_id=row.id,
            kind=TriggerKind.RSS,
            name="feed",
            is_active=True,
            config={"url": "https://example.com/feed.xml"},
        )
    )
    db.commit()

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "Gitlab" not in caplog.text
    assert dispatched == []


def test_a_paused_project_with_an_unscannable_url_is_not_stranded(
    db, user, dispatched, stub_github, caplog
):
    """Autopilot off means it is not waiting to scan — nothing to warn about."""
    _project(db, user, "Dormant", repo_url="https://gitlab.com/acme/thing",
             mode=AutopilotMode.OFF)

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "Dormant" not in caplog.text


def test_an_unscannable_project_is_not_counted_as_held_by_its_interval(
    db, user, dispatched, stub_github, caplog
):
    """Two different silences, and the log must not merge them.

    "Held back by their own scan interval" is the interval working as set. A
    project that can never scan is a configuration fault. Counting the second as
    the first would put a broken project inside a reassuring sentence.
    """
    _project(db, user, "gitlab", repo_url="https://gitlab.com/acme/thing")

    with caplog.at_level("INFO"):
        autopilot_tasks.scan_all_projects()

    assert "held back by their own scan interval" not in caplog.text


def test_the_interval_still_holds_a_scannable_project(
    db, user, dispatched, stub_github, caplog
):
    """The other side of the count above, so the filter cannot swallow it."""
    _project(
        db,
        user,
        "pulse",
        repo_url="https://github.com/r2st/DoAide-Pulse",
        interval=48,
        last_scanned_at=utcnow() - timedelta(hours=1),
    )

    with caplog.at_level("INFO"):
        autopilot_tasks.scan_all_projects()

    assert dispatched == []
    assert "held back by their own scan interval" in caplog.text
