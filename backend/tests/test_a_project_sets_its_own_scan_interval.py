"""How often *this* repo is looked at, as opposed to how often the sweep runs.

Before ``autopilot_min_interval_hours`` the only control over autopilot
frequency was ``AUTOPILOT_SCAN_INTERVAL_SECONDS``, which is one number for the
whole deployment. "Write about the docs repo at most twice a week" could
therefore only be expressed by slowing every project on the box to that rate.

The two knobs beside it do not cover it either, and the distinction is what
these tests pin: ``autopilot_commit_threshold`` gates on *how much* has shipped
rather than how long it has been, and ``autopilot_daily_content_limit`` is a
ceiling on a single day, so a repo that clears the threshold hourly still writes
on every day it possibly can.

Two things are deliberately *not* held by the interval, and both have a test
below: a human pressing Scan, who is asking for this repo to be looked at now
and knows something the interval does not; and a project that has never been
scanned, because an interval is a gap between scans and there is no first scan
to measure a gap from.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Project, Tone, scan_due
from app.tasks import autopilot_tasks

# --------------------------------------------------------------------------- #
# scan_due, which is the whole of the policy                                   #
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_a_project_with_no_interval_is_always_due():
    """0 is the default and means "every sweep" — what every project did before."""
    assert scan_due(NOW - timedelta(minutes=1), 0, NOW) is True


def test_a_project_that_has_never_scanned_is_due_however_long_the_interval():
    """An interval is a gap between two scans. There is no first scan to measure from.

    Without this a project created with a 48-hour interval would wait two days
    before its first scan, which reads from outside as an autopilot that does
    not work.
    """
    assert scan_due(None, 720, NOW) is True


def test_a_project_scanned_inside_its_interval_is_held():
    assert scan_due(NOW - timedelta(hours=5), 24, NOW) is False


def test_a_project_scanned_longer_ago_than_its_interval_is_due():
    assert scan_due(NOW - timedelta(hours=25), 24, NOW) is True


def test_the_boundary_is_inclusive():
    """Exactly one interval ago is due, not held.

    The sweep runs on its own timer, so a project whose interval is a whole
    multiple of the sweep period lands exactly on this boundary every time. An
    exclusive comparison would push it to the *next* tick on every cycle, and a
    24-hour interval would quietly become 25.
    """
    assert scan_due(NOW - timedelta(hours=24), 24, NOW) is True


def test_a_naive_timestamp_is_read_as_utc():
    """SQLite hands back naive datetimes; PostgreSQL hands back aware ones.

    The comparison is against an aware ``now``, so a naive value has to be
    adopted rather than compared — otherwise this raises ``TypeError`` on the
    test database and works in production, which is the worst of both.
    """
    assert scan_due(datetime(2026, 8, 15, 11, 0), 24, NOW) is False
    assert scan_due(datetime(2026, 8, 10, 11, 0), 24, NOW) is True


def test_a_negative_interval_does_not_hold_anything_back():
    """Defence in depth: the schema bounds this at >= 0, the column does not."""
    assert scan_due(NOW, -5, NOW) is True


def test_the_model_method_and_the_free_function_agree():
    """``Project.scan_due`` delegates, so the sweep and the row cannot drift.

    The sweep reads three columns rather than whole rows and so calls the free
    function; anything holding a real ``Project`` calls the method. Two
    implementations of this would eventually disagree about one project.
    """
    row = Project(last_scanned_at=NOW - timedelta(hours=5), autopilot_min_interval_hours=24)
    assert row.scan_due(NOW) is scan_due(row.last_scanned_at, 24, NOW) is False


def test_scan_due_defaults_to_the_current_clock():
    """The argument is optional; omitting it must not mean "never due"."""
    assert scan_due(utcnow() - timedelta(hours=48), 24) is True


# --------------------------------------------------------------------------- #
# The sweep, which is the only caller that honours it                          #
# --------------------------------------------------------------------------- #


def _project(db, user, slug, *, interval=0, last_scanned_at=None):
    row = Project(
        user_id=user.id,
        name=slug,
        slug=slug,
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
        repo_url=f"https://github.com/r2st/{slug}",
        autopilot_mode=AutopilotMode.DRAFT,
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

    class _NoClose:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr(autopilot_tasks, "SessionLocal", lambda: _NoClose())
    return calls


def test_the_sweep_skips_a_project_still_inside_its_interval(db, user, dispatched):
    _project(db, user, "held", interval=48, last_scanned_at=utcnow() - timedelta(hours=1))

    result = autopilot_tasks.scan_all_projects()

    assert dispatched == []
    # "scanned" counts what the sweep decided to scan, not what it looked at and
    # rejected. A held project is not a scan that happened.
    assert result["scanned"] == 0


def test_the_sweep_takes_a_project_whose_interval_has_elapsed(db, user, dispatched):
    row = _project(
        db, user, "due", interval=24, last_scanned_at=utcnow() - timedelta(hours=30)
    )

    autopilot_tasks.scan_all_projects()

    assert dispatched == [row.id]


def test_one_projects_interval_does_not_hold_another_back(db, user, dispatched):
    """The point of the column: per project, not per deployment."""
    held = _project(
        db, user, "slow", interval=72, last_scanned_at=utcnow() - timedelta(hours=2)
    )
    fast = _project(db, user, "fast", interval=0, last_scanned_at=utcnow())

    autopilot_tasks.scan_all_projects()

    assert dispatched == [fast.id]
    assert held.id not in dispatched


def test_the_sweep_says_how_many_it_held(db, user, dispatched, caplog):
    """"The autopilot has gone quiet" and "the autopilot is on a long interval"
    look identical from outside, and the first of those is a bug report."""
    _project(db, user, "held", interval=48, last_scanned_at=utcnow() - timedelta(hours=1))

    with caplog.at_level("INFO"):
        autopilot_tasks.scan_all_projects()

    assert "held back by their own scan interval" in caplog.text


def test_a_hand_run_scan_ignores_the_interval(db, user, stub_github, monkeypatch):
    """A person pressing Scan knows something the interval does not.

    The interval bounds the *automated* cadence. Routing a deliberate request
    through it would mean a project on a weekly interval could not be scanned
    on demand at all, which is the one moment somebody actually needs it.
    """
    row = _project(
        db, user, "held", interval=720, last_scanned_at=utcnow() - timedelta(minutes=1)
    )

    result = autopilot_tasks.scan_project(row.id)

    assert result["status"] != "held"
    assert stub_github["calls"], "the repo was never read"


# --------------------------------------------------------------------------- #
# The API surface                                                              #
# --------------------------------------------------------------------------- #


def test_the_interval_round_trips_through_the_api(client, auth, project):
    patched = client.patch(
        f"/api/v1/projects/{project.id}",
        json={"autopilot_min_interval_hours": 48},
        headers=auth,
    )

    assert patched.status_code == 200
    assert patched.json()["autopilot_min_interval_hours"] == 48
    assert (
        client.get(f"/api/v1/projects/{project.id}", headers=auth).json()[
            "autopilot_min_interval_hours"
        ]
        == 48
    )


def test_a_project_created_without_one_scans_every_sweep(client, auth):
    """The default has to be the old behaviour, or this column changes every
    existing project's cadence the moment it ships."""
    created = client.post(
        "/api/v1/projects",
        json={"name": "Fresh", "repo_url": "https://github.com/r2st/fresh"},
        headers=auth,
    )

    assert created.status_code == 201
    assert created.json()["autopilot_min_interval_hours"] == 0


@pytest.mark.parametrize("bad", [-1, 721])
def test_an_interval_off_the_scale_is_refused(client, auth, project, bad):
    """Negative is not a faster autopilot and 30 days is not an interval, it is
    ``autopilot_mode = off`` written as a number nobody would recognise."""
    refused = client.patch(
        f"/api/v1/projects/{project.id}",
        json={"autopilot_min_interval_hours": bad},
        headers=auth,
    )

    assert refused.status_code == 422
