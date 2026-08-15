"""A deploy must not be an outage for whoever was mid-request.

Every unit under ``deploy/systemd`` is restarted by ``deploy/deploy.sh`` on
every push. Stopping a service means SIGTERM, then — after ``TimeoutStopSec`` —
SIGKILL to whatever is left. Both halves have to be set for the stop to be a
drain rather than a cut:

* the process needs a **bounded** graceful window, or it waits forever for the
  one request that never finishes and systemd eventually kills every *other*
  in-flight request along with it;
* that window has to be **shorter than** the kill timeout, or it never gets to
  finish on its own terms and the bound was decorative.

None of that is Python, and none of it fails in a way anyone sees until a
deploy drops requests on the floor. So the arithmetic is asserted here, where a
change to an ``ExecStart`` line has to answer for it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_UNITS = Path(__file__).resolve().parents[2] / "deploy" / "systemd"


def _unit(name: str) -> str:
    return (_UNITS / f"herald-{name}.service").read_text()


def _directive(unit: str, key: str) -> str | None:
    """The last value assigned to *key*, the way systemd reads a unit file."""
    found = re.findall(rf"^{key}=(.*)$", unit, flags=re.MULTILINE)
    return found[-1].strip() if found else None


#: The units a deploy stops and starts, which is what everything below is about.
#: ``herald-backup`` is deliberately not among them: it is ``Type=oneshot``, it
#: is started by a timer rather than by ``deploy.sh``, and a drain window is
#: meaningless for a process whose whole life is one ``pg_dump``. Its own
#: invariants are asserted in ``test_backups_are_taken_and_restorable``.
_LONG_RUNNING = ("api", "beat", "web", "worker")


def test_the_units_are_where_the_tests_think_they_are():
    """A guard on the path: an empty glob would pass every test below."""
    assert sorted(p.name for p in _UNITS.glob("*.service")) == sorted(
        [f"herald-{name}.service" for name in _LONG_RUNNING]
        + ["herald-backup.service"]
    )


def test_the_api_drains_in_flight_requests_before_it_stops():
    """Without the flag uvicorn cuts connections at SIGTERM."""
    exec_start = _directive(_unit("api"), "ExecStart")

    assert "--timeout-graceful-shutdown" in exec_start


def test_the_api_finishes_draining_before_systemd_gives_up_on_it():
    """The drain window has to fit inside the kill window, with room to spare.

    The lifespan hook that disposes the connection pool runs *after* the drain,
    so equal timeouts would mean the pool is disposed by SIGKILL, which is to
    say not disposed.
    """
    unit = _unit("api")
    graceful = int(
        re.search(r"--timeout-graceful-shutdown\s+(\d+)", _directive(unit, "ExecStart"))
        .group(1)
    )
    stop = int(_directive(unit, "TimeoutStopSec"))

    assert graceful < stop


@pytest.mark.parametrize("service", ["worker", "beat"])
def test_the_celery_units_stop_their_own_children(service):
    """``KillMode=mixed``, so the pool is not killed out from under the parent.

    systemd's default signals every process in the cgroup at once. Celery's warm
    shutdown depends on the parent doing the signalling itself, in order.
    """
    assert _directive(_unit(service), "KillMode") == "mixed"


@pytest.mark.parametrize("service", ["worker", "beat"])
def test_the_celery_units_bound_how_long_they_may_take(service):
    """A stop with no bound is a deploy that hangs instead of a task that dies."""
    stop = _directive(_unit(service), "TimeoutStopSec")

    assert stop is not None and int(stop) > 0


def test_the_worker_is_given_longer_to_stop_than_beat():
    """Not a style preference — it is what the two of them do.

    The worker may be mid-publish against a platform API and is worth waiting
    for. Beat only decides when things run; there is nothing in flight to lose,
    and holding a deploy for it buys nothing.
    """
    assert int(_directive(_unit("worker"), "TimeoutStopSec")) > int(
        _directive(_unit("beat"), "TimeoutStopSec")
    )
