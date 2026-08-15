"""A sweep doing the work itself has to watch its own clock.

All three per-row beat sweeps answer an unreachable broker the same way: rather
than drop the batch, they run each row *inline*, on the beat task's own thread
and inside its own soft time limit. That trade is deliberate and tested
elsewhere. What is here is the second half of it, which is what happens when the
inline work is what runs out of time.

The soft limit does not arrive in the sweep as an exception. Celery raises
``SoftTimeLimitExceeded`` once, inside whatever is running — and what is running
is the per-row task, every one of which catches it deliberately, because each
has something it must settle before it stops:

* ``publish_one`` records the timeout on the publication row;
* ``scan_project`` leaves the commit watermark where it is, so the commits it
  never wrote about are not silently skipped;
* ``check_trigger`` reports the firing as timed out rather than crashed.

So by the time control is back in the sweep's loop, the only surviving trace of
the deadline is the ``{"status": "timeout"}`` the row returned. Read as an
ordinary result — which is what two of these three sweeps did — the sweep goes
on to the next row and starts a fresh repo read, model call or feed fetch
*after* the deadline, and the one after that, until the hard limit kills the
worker outright. ``task_acks_late`` then redelivers the whole batch to the
replacement worker, which selects the same rows in the same order and does it
again.

``publish_due`` has had this branch and a test since the same bug was found
there. These are the other two.

The neighbouring case is here too: the sweeps call the per-row task directly on
this path, and its "never raises" promise is made by a ``try`` it enters *after*
opening its session — so ``SessionLocal()`` and the ``close()`` in its
``finally`` are outside the promise and land in the sweep's loop. Escaping there
would not lose one row, it would drop every later row in the batch, and the next
pass would die in the same place.
"""
from __future__ import annotations

import pytest

from app.models.project import AutopilotMode, Project, Tone
from app.models.trigger import Trigger, TriggerKind
from app.tasks import autopilot_tasks, trigger_tasks


def _no_close(session):
    """The task's ``SessionLocal``, minus the ``close()`` that ends the fixture."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


class _BrokerDown:
    """A task whose ``.delay`` refuses, so the sweep falls back to inline.

    ``__call__`` is the inline path, and it answers with whatever *outcomes*
    says — one entry per call, so a test can say "the second one times out".
    """

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[int] = []

    def delay(self, _row_id):
        raise ConnectionError("redis refused the connection")

    def __call__(self, row_id):
        self.calls.append(row_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --------------------------------------------------------------------------- #
# scan_all_projects                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def three_projects(db, user) -> list[Project]:
    """Three scannable projects, so "stopped part-way" is distinguishable."""
    rows = []
    for i in range(3):
        row = Project(
            user_id=user.id,
            name=f"Repo {i}",
            slug=f"repo-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
            repo_url=f"https://github.com/r2st/repo-{i}",
            autopilot_mode=AutopilotMode.DRAFT,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


@pytest.fixture
def autopilot_session(db, monkeypatch):
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", _no_close(db))


def test_an_inline_scan_that_times_out_stops_the_autopilot_sweep(
    db, three_projects, autopilot_session, monkeypatch
):
    """One repo read past the deadline is a mistake; the rest are the outage.

    The remaining projects keep their watermarks and their commits, so the next
    scheduled scan picks them up with nothing lost.
    """
    scan = _BrokerDown(
        [
            {"project_id": 1, "status": "no_news"},
            {"project_id": 2, "status": "timeout"},
            {"project_id": 3, "status": "no_news"},
        ]
    )
    monkeypatch.setattr(autopilot_tasks, "scan_project", scan)

    result = autopilot_tasks.scan_all_projects()

    assert len(scan.calls) == 2, "the third project must not be scanned after the break"
    assert result == {"scanned": 3, "dispatched": 2, "failed": 0}


def test_the_autopilot_sweep_says_it_ran_out_of_time_scanning(
    db, three_projects, autopilot_session, monkeypatch, caplog
):
    """The one line an operator has to go on.

    "dispatched 2 of 3" alone reads as a sweep that chose to stop. It did not
    choose — it ran past its own deadline doing work a worker should have taken.
    """
    scan = _BrokerDown([{"status": "timeout"}, {}, {}])
    monkeypatch.setattr(autopilot_tasks, "scan_project", scan)

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "timed out scanning inline after 1 of 3" in caplog.text


def test_one_inline_scan_crashing_does_not_end_the_autopilot_sweep(
    db, three_projects, autopilot_session, monkeypatch
):
    """``scan_project`` opens its session outside its own ``try``.

    Whatever escapes it lands here, and taking the loop down with it would drop
    every later project in the fleet rather than the one that failed.
    """
    scan = _BrokerDown(
        [
            RuntimeError("the session would not open"),
            {"status": "no_news"},
            {"status": "no_news"},
        ]
    )
    monkeypatch.setattr(autopilot_tasks, "scan_project", scan)

    result = autopilot_tasks.scan_all_projects()

    assert len(scan.calls) == 3, "the projects behind the bad one still scanned"
    assert result == {"scanned": 3, "dispatched": 2, "failed": 1}


def test_a_crashed_inline_scan_is_not_counted_as_dispatched(
    db, three_projects, autopilot_session, monkeypatch, caplog
):
    """A sweep that drops a project must not report having placed it.

    ``dispatched`` is what was handed on or done, not what was selected — the
    same distinction ``publish_due`` draws. Counting the failure would leave the
    only record of it in a log line nobody is reading.
    """
    scan = _BrokerDown([RuntimeError("boom")] * 3)
    monkeypatch.setattr(autopilot_tasks, "scan_project", scan)

    with caplog.at_level("ERROR"):
        result = autopilot_tasks.scan_all_projects()

    assert result == {"scanned": 3, "dispatched": 0, "failed": 3}
    assert "inline scan of project" in caplog.text


# --------------------------------------------------------------------------- #
# check_due_triggers                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def three_triggers(db, user) -> list[Trigger]:
    """Three due schedule triggers on one project."""
    project = Project(
        user_id=user.id,
        name="Triggered",
        slug="triggered",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(project)
    db.flush()
    rows = []
    for i in range(3):
        row = Trigger(
            project_id=project.id,
            kind=TriggerKind.SCHEDULE,
            name=f"Every day {i}",
            config={"interval_hours": 24},
            is_active=True,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


@pytest.fixture
def trigger_session(db, monkeypatch):
    monkeypatch.setattr(trigger_tasks, "SessionLocal", _no_close(db))


def test_an_inline_check_that_times_out_stops_the_trigger_sweep(
    db, three_triggers, trigger_session, monkeypatch
):
    """A feed fetch started after the deadline is the worker's next outage.

    The triggers behind it stay due, and the next tick dispatches them.
    """
    check = _BrokerDown(
        [
            {"trigger_id": 1, "status": "fired"},
            {"trigger_id": 2, "status": "timeout"},
            {"trigger_id": 3, "status": "fired"},
        ]
    )
    monkeypatch.setattr(trigger_tasks, "check_trigger", check)

    result = trigger_tasks.check_due_triggers()

    assert len(check.calls) == 2, "the third trigger must not be polled after the break"
    assert result["dispatched"] == 2
    assert result["failed"] == 0


def test_the_trigger_sweep_says_it_ran_out_of_time_checking(
    db, three_triggers, trigger_session, monkeypatch, caplog
):
    check = _BrokerDown([{"status": "timeout"}, {}, {}])
    monkeypatch.setattr(trigger_tasks, "check_trigger", check)

    with caplog.at_level("WARNING"):
        trigger_tasks.check_due_triggers()

    assert "timed out checking inline after 1 of 3" in caplog.text


def test_one_inline_check_crashing_does_not_end_the_trigger_sweep(
    db, three_triggers, trigger_session, monkeypatch
):
    check = _BrokerDown(
        [RuntimeError("the session would not open"), {"status": "fired"}, {}]
    )
    monkeypatch.setattr(trigger_tasks, "check_trigger", check)

    result = trigger_tasks.check_due_triggers()

    assert len(check.calls) == 3, "the triggers behind the bad one were still checked"
    assert result["dispatched"] == 2
    assert result["failed"] == 1


def test_a_crashed_inline_check_is_not_counted_as_dispatched(
    db, three_triggers, trigger_session, monkeypatch, caplog
):
    check = _BrokerDown([RuntimeError("boom")] * 3)
    monkeypatch.setattr(trigger_tasks, "check_trigger", check)

    with caplog.at_level("ERROR"):
        result = trigger_tasks.check_due_triggers()

    assert result["due"] == 3
    assert result["dispatched"] == 0
    assert result["failed"] == 3
    assert "inline check of trigger" in caplog.text


# --------------------------------------------------------------------------- #
# The three sweeps agree                                                       #
# --------------------------------------------------------------------------- #


def test_a_timeout_is_still_told_apart_from_a_dead_broker(
    db, three_projects, autopilot_session, monkeypatch, caplog
):
    """The two warnings must not be confused for one another.

    Running out of time is not the broker being down, and an operator reading
    "broker unavailable" would go and look at Redis. The broker *is* down here —
    that is how the sweep got onto the inline path at all — so both lines
    appear, and each says its own thing.
    """
    scan = _BrokerDown([{"status": "timeout"}, {}, {}])
    monkeypatch.setattr(autopilot_tasks, "scan_project", scan)

    with caplog.at_level("WARNING"):
        autopilot_tasks.scan_all_projects()

    assert "broker unavailable" in caplog.text
    assert "timed out scanning inline" in caplog.text
    # And not the *dispatch* timeout, which is the other branch entirely: that
    # one fires when the limit lands during ``.delay()`` and must never reach
    # the inline fallback at all.
    assert "autopilot dispatch timed out" not in caplog.text


@pytest.mark.parametrize(
    ("module", "task_attr"),
    [
        ("publish_tasks", "publish_one"),
        ("autopilot_tasks", "scan_project"),
        ("trigger_tasks", "check_trigger"),
    ],
)
def test_every_per_row_task_reports_a_timeout_rather_than_raising(module, task_attr):
    """What the sweeps above are reading is a promise the row tasks make.

    Each catches its own ``SoftTimeLimitExceeded`` so it can settle its row, and
    that is precisely why the sweep cannot learn about the deadline from an
    exception. If one of them ever stopped catching it, the ``status`` checks
    above would go quietly dead — they would simply never see ``"timeout"``.
    """
    import importlib
    import inspect

    source = inspect.getsource(
        getattr(importlib.import_module(f"app.tasks.{module}"), task_attr)
    )

    assert "SoftTimeLimitExceeded" in source
    assert '"timeout"' in source
