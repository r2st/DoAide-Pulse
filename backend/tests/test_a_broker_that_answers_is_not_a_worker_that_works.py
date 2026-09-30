"""Redis answering PING is not the same fact as anything consuming the queue.

The broker is a queue. It accepts everything whether or not a worker is
draining it, so a worker that was OOM-killed or never came back from a deploy
leaves ``/health`` entirely green while nothing publishes, no repo is scanned,
no trigger fires and no webhook is delivered — Herald's whole product runs in
that process. The person asking "why has nothing gone out since Tuesday?" was
shown two healthy dependencies and left to guess.

``tests.test_health`` covers the two probes that decide the status code. This
covers the one that deliberately does *not*, and why:

* it lives on ``/health/detail``, behind auth, because the public endpoint is
  Caddy's ``health_uri`` — failing it over a dead background worker would pull
  the API out of rotation and take down the UI somebody needs in order to find
  out what happened;
* and because the probe is a *broadcast* over the broker with a wait attached,
  which is not a thing to put on an endpoint anonymous callers may hit sixty
  times a minute, pointed at the queue the workers are trying to read.
"""
from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.routers import misc

HEALTH = "/api/v1/health"


@pytest.fixture
def redis_up(monkeypatch):
    monkeypatch.setattr(misc, "_check_redis", lambda: misc._Probe(True))


@pytest.fixture
def celery_on(monkeypatch):
    """Tests run with ``CELERY_ENABLED=false``; this is the deployed shape."""
    monkeypatch.setattr(settings, "celery_enabled", True)


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.routers.misc" and r.levelno == logging.WARNING
    ]


def _ping(monkeypatch, result):
    """Stand in for the broadcast, so no test waits on a real broker."""
    calls: list[float] = []

    class _Control:
        @staticmethod
        def ping(timeout):
            calls.append(timeout)
            if isinstance(result, Exception):
                raise result
            return result

    class _App:
        control = _Control()

    module = type(misc)("app.tasks.celery_app")
    module.celery_app = _App()
    monkeypatch.setitem(__import__("sys").modules, "app.tasks.celery_app", module)
    return calls


# --------------------------------------------------------------------------- #
# The probe                                                                    #
# --------------------------------------------------------------------------- #


def test_a_worker_that_answers_is_reported_healthy(monkeypatch, celery_on):
    _ping(monkeypatch, [{"celery@box": {"ok": "pong"}}])

    assert misc._check_workers().ok


def test_a_silent_queue_is_reported_even_though_redis_is_fine(monkeypatch, celery_on):
    """The whole point: an empty reply list, with the broker perfectly healthy."""
    _ping(monkeypatch, [])
    probe = misc._check_workers()

    assert not probe.ok
    assert "no worker" in probe.detail


def test_a_broker_that_will_not_take_the_broadcast_is_not_an_exploding_probe(
    monkeypatch, celery_on
):
    """A health check that raises is worse than one that reports a failure."""
    _ping(monkeypatch, ConnectionResetError("broker went away"))
    probe = misc._check_workers()

    assert not probe.ok
    assert "ConnectionResetError" in probe.detail


def test_the_broadcast_is_bounded_by_the_health_timeout(monkeypatch, celery_on):
    """With nothing listening this waits the timeout out in full, so it must
    be the *configured* one and not Celery's own default."""
    calls = _ping(monkeypatch, [])
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 0.5)

    misc._check_workers()

    assert calls == [0.5]


def test_no_broadcast_at_all_when_tasks_run_inline(monkeypatch):
    """``celery_enabled`` off means tasks run in the API process.

    There is no worker to look for, so "unavailable" would be a false alarm
    about the configuration the operator chose — and sending a broadcast to
    find that out would be worse than pointless.
    """
    calls = _ping(monkeypatch, [])
    monkeypatch.setattr(settings, "celery_enabled", False)

    assert misc._check_workers() is None
    assert calls == []


# --------------------------------------------------------------------------- #
# What it is allowed to do to the status code                                  #
# --------------------------------------------------------------------------- #


def test_a_dead_worker_does_not_take_the_site_out_of_rotation(
    client, auth, redis_up, celery_on, monkeypatch
):
    """The assertion this whole design turns on.

    A dead worker is a real outage and not *this* endpoint's kind of one. The
    API is still serving, and answering 503 here would have Caddy stop routing
    to it — turning a background failure into a total one, and taking away the
    page the operator would use to diagnose it.
    """
    _ping(monkeypatch, [])

    resp = client.get(f"{HEALTH}/detail", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["workers"]["status"] == "unavailable"
    assert body["workers"]["required"] is False


def test_the_public_probe_neither_reports_nor_broadcasts(
    client, redis_up, celery_on, monkeypatch
):
    """Caddy's endpoint stays exactly as cheap and as green as it was."""
    calls = _ping(monkeypatch, [])

    resp = client.get(HEALTH)

    assert resp.status_code == 200
    assert "workers" not in resp.json()
    assert calls == [], "the anonymous endpoint broadcast to the broker"


def test_a_live_worker_shows_up_for_the_logged_in_caller(
    client, auth, redis_up, celery_on, monkeypatch
):
    _ping(monkeypatch, [{"celery@box": {"ok": "pong"}}])
    body = client.get(f"{HEALTH}/detail", headers=auth).json()

    assert body["workers"] == {"status": "ok", "required": False, "detail": ""}


def test_inline_mode_says_disabled_rather_than_broken(client, auth, redis_up):
    """``celery_enabled`` is off in tests, which is the inline shape."""
    body = client.get(f"{HEALTH}/detail", headers=auth).json()

    assert body["workers"]["status"] == "disabled"
    assert body["workers"]["required"] is False


def test_a_real_outage_still_fails_the_check_with_a_dead_worker_beside_it(
    client, auth, celery_on, monkeypatch
):
    """The worker probe must not *mask* a required dependency either."""
    monkeypatch.setattr(
        misc, "_check_redis", lambda: misc._Probe(False, "ConnectionError")
    )
    _ping(monkeypatch, [])

    resp = client.get(f"{HEALTH}/detail", headers=auth)

    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


# --------------------------------------------------------------------------- #
# What it must leave behind                                                    #
# --------------------------------------------------------------------------- #


def test_a_silent_queue_is_written_to_the_log_and_not_only_to_the_body(
    client, auth, redis_up, celery_on, monkeypatch, caplog
):
    """The one probe in this file whose failure existed only in a response.

    ``_health_core`` writes a line whenever the database or Redis is down. This
    one did not, and it is the failure with the least else to find it: the worker
    fleet being gone means no task writes a line either, so a journal spanning
    the whole outage contained nothing about it at all — and the single moment
    Herald *knew* the fleet was missing went unrecorded.
    """
    _ping(monkeypatch, [])

    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        assert client.get(f"{HEALTH}/detail", headers=auth).status_code == 200

    lines = _warnings(caplog)
    assert len(lines) == 1
    assert "worker probe failed" in lines[0]
    assert "no worker answered" in lines[0]


def test_a_broker_that_refused_the_broadcast_names_the_error_in_the_log(
    client, auth, redis_up, celery_on, monkeypatch, caplog
):
    """The authenticated view keeps the message; so does the line beside it."""
    _ping(monkeypatch, ConnectionResetError("broker went away"))

    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        client.get(f"{HEALTH}/detail", headers=auth)

    line = _warnings(caplog)[0]
    assert "ConnectionResetError" in line
    assert "broker went away" in line


def test_a_healthy_fleet_writes_nothing(
    client, auth, redis_up, celery_on, monkeypatch, caplog
):
    """This endpoint is polled by whoever is watching; green must stay quiet."""
    _ping(monkeypatch, [{"celery@box": {"ok": "pong"}}])

    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        client.get(f"{HEALTH}/detail", headers=auth)

    assert _warnings(caplog) == []


def test_inline_mode_is_not_reported_as_a_missing_fleet(client, auth, redis_up, caplog):
    """``celery_enabled`` off is the configuration, not a failure of it."""
    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        client.get(f"{HEALTH}/detail", headers=auth)

    assert _warnings(caplog) == []


def test_the_worker_line_is_not_the_degraded_line(
    client, auth, celery_on, monkeypatch, caplog
):
    """Two independent facts, two lines: the 503 must not absorb the worker one.

    A required dependency down *and* a dead fleet is the shape of a box that has
    just been rebooted, and an operator reading only "health check failed" would
    restart Postgres and stop looking.
    """
    down = misc._Probe(False, "ConnectionError", "ConnectionError: refused")
    monkeypatch.setattr(misc, "_check_redis", lambda: down)
    _ping(monkeypatch, [])

    with caplog.at_level(logging.WARNING, logger="app.routers.misc"):
        assert client.get(f"{HEALTH}/detail", headers=auth).status_code == 503

    lines = _warnings(caplog)
    assert len(lines) == 2
    assert any("health check failed" in m for m in lines)
    assert any("worker probe failed" in m for m in lines)
