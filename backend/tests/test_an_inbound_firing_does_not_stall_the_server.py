"""``POST /triggers/inbound/{token}`` used to do its work on the event loop.

It is the only ``async def`` endpoint in the API — it has to be, because reading
the raw body needs an ``await`` — and it was also, by a wide margin, the slowest
thing any route does. A firing runs :func:`app.services.triggers.fire`, which
generates a piece: blocking ``httpx.post`` calls to the LLM chain, up to
``llm_max_attempts`` sweeps of four providers with a rate-limit sleep of up to
``llm_retry_max_backoff_seconds`` between them, a link check per outbound link,
then publish dispatch. Awaited straight from the handler, every second of that
was a second the worker's event loop could not do anything else.

That is not a slow endpoint. It is an outage with a slow endpoint as its
trigger: one firing stops the process serving *every* other request — dashboards,
logins, and Caddy's ``/health`` poll, which is how it becomes an unhealthy
backend rather than a delay somebody waits out. Two uvicorn workers, so two
concurrent firings take the deployment down, and the endpoint that does it is
unauthenticated by design.

The fix is the arrangement every other route in this app already has: the loop
reads the body, and the blocking half runs on Starlette's worker threads. These
tests pin that, and pin that the answers did not change while it moved.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil

from fastapi.routing import APIRoute

import app.routers as routers_package
from app.database import get_db
from app.models.project import AutopilotMode
from app.models.trigger import Trigger
from app.routers import triggers as triggers_router
from app.services import triggers as trigger_service

API = "/api/v1/triggers"


def _every_route() -> list[APIRoute]:
    """Every endpoint in the API, read off the routers rather than the app.

    ``create_app().routes`` is the obvious source and the wrong one: FastAPI
    leaves an ``_IncludedRouter`` placeholder per ``include_router`` and only
    expands it later, so a freshly built app answers with ``GET /`` and four
    doc routes. A sweep written against that reports success having examined
    one endpoint, which is a worse outcome than not having the sweep.

    Walking the package picks up a new router module on its own, so this does
    not need updating when one is added — only the ``__init__``-style private
    modules are skipped.
    """
    routes: list[APIRoute] = []
    for module_info in pkgutil.iter_modules(routers_package.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"app.routers.{module_info.name}")
        router = getattr(module, "router", None)
        if router is None:
            continue
        routes.extend(r for r in router.routes if isinstance(r, APIRoute))
    return routes


def _create(client, auth, project, **config):
    return client.post(
        API,
        json={
            "project_id": project.id,
            "kind": "webhook",
            "name": "CI",
            "config": config,
        },
        headers=auth,
    )


def _token(client, auth, project, db, **config) -> str:
    created = _create(client, auth, project, **config).json()
    return db.get(Trigger, created["id"]).token


def _on_the_event_loop() -> bool:
    """Whether this call is running on the thread driving the event loop.

    ``get_running_loop`` is per-thread and raises when there is not one, which
    makes it the whole test: on a worker thread there is no running loop, and on
    the loop's own thread there is. Nothing here has to know *which* thread is
    which, only that the caller is not the one that must never block.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# --------------------------------------------------------------------------- #
# The slow half runs off the loop                                              #
# --------------------------------------------------------------------------- #


def test_generating_a_piece_does_not_run_on_the_event_loop(
    client, auth, project, db, monkeypatch
):
    """:func:`triggers.fire` is where the LLM call is. It must not be on the loop.

    This is the assertion the whole module exists for. Everything below is a
    guard against it regressing somewhere adjacent.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    token = _token(client, auth, project, db, headline_path="title")

    seen: dict[str, bool] = {}

    def _fire(db_, trigger, signal):
        seen["on_loop"] = _on_the_event_loop()
        return None  # a duplicate: settles the request without generating

    monkeypatch.setattr(trigger_service, "fire", _fire)

    resp = client.post(f"{API}/inbound/{token}", json={"title": "We shipped"})

    assert resp.status_code == 202, resp.text
    assert seen, "trigger_service.fire was never reached"
    assert seen["on_loop"] is False, (
        "the firing ran on the event loop — a generation now blocks every other "
        "request this worker is serving"
    )


def test_the_trigger_lookup_does_not_run_on_the_event_loop(
    client, auth, project, db, monkeypatch
):
    """The ``SELECT`` moved too, and it is not a rounding error.

    ``db_statement_timeout_seconds`` is 30. A lookup against a database having a
    bad moment is up to thirty seconds of a worker serving nothing — from the
    cheapest-looking line in the handler.
    """
    token = _token(client, auth, project, db)

    seen: dict[str, bool] = {}
    original = trigger_service.signal_from_webhook

    def _signal(trigger, body):
        # Called immediately after the lookup, in the same function, so its
        # thread is the lookup's thread.
        seen["on_loop"] = _on_the_event_loop()
        return original(trigger, body)

    monkeypatch.setattr(trigger_service, "signal_from_webhook", _signal)

    resp = client.post(f"{API}/inbound/{token}", json={"any": "body"})

    assert resp.status_code == 202, resp.text
    assert seen.get("on_loop") is False


def test_no_async_route_touches_the_database_on_the_event_loop():
    """A sweep, so the next ``async def`` route cannot reintroduce this quietly.

    Any endpoint that is a coroutine function *and* depends on ``get_db`` is
    holding a synchronous ``Session``, and every statement it emits blocks the
    loop. The only acceptable shape is the one :func:`triggers.inbound` now has:
    hand the work to a thread. Adding a route that does neither fails here
    rather than in production, where it shows up as the whole worker going
    quiet and nothing in the logs saying why.
    """
    routes = _every_route()
    # The enumeration is half the test. A sweep that finds nothing to check
    # reports success forever — see :func:`_every_route` for how easy that is to
    # write by accident.
    assert len(routes) > 80, f"only found {len(routes)} routes; the walk is wrong"
    assert any(route.path.endswith("/inbound/{token}") for route in routes), (
        "the inbound endpoint is not in the sweep, so this is not looking at "
        "the route it was written for"
    )

    offenders = []
    for route in routes:
        endpoint = route.endpoint
        if not inspect.iscoroutinefunction(endpoint):
            continue
        source = inspect.getsource(endpoint)
        if "get_db" not in source:
            continue
        if "run_in_threadpool" in source:
            continue
        offenders.append(f"{sorted(route.methods)} {route.path} ({endpoint.__name__})")

    assert not offenders, (
        "async route(s) doing synchronous database work on the event loop: "
        + ", ".join(offenders)
        + " — make the route a plain `def`, or hand the blocking half to "
        "`run_in_threadpool` the way triggers.inbound does"
    )


def test_the_dependency_is_still_the_request_scoped_session():
    """The sweep above keys on ``get_db`` by name, so it has to be the real one."""
    assert get_db.__module__ == "app.database"
    assert "get_db" in inspect.getsource(triggers_router.inbound)


# --------------------------------------------------------------------------- #
# The answers did not change while the work moved threads                      #
# --------------------------------------------------------------------------- #


def test_an_unknown_token_is_still_refused_before_the_body_is_judged(client):
    """404 outranks 413.

    The two checks swapped places in the source when the handler split, and the
    order is observable: a caller holding a stale URL learns "no such trigger",
    not "your payload is too big", which would confirm the token was read and
    found wanting for some other reason.
    """
    oversized = {"text": "x" * (triggers_router.MAX_INBOUND_BYTES + 1024)}

    resp = client.post(f"{API}/inbound/not-a-real-token", json=oversized)

    assert resp.status_code == 404


def test_a_body_over_the_inbound_cap_is_refused(client, auth, project, db):
    """128 KB, below the 1 MB the body-size middleware allows.

    The middleware's cap is about what the server will buffer; this one is about
    what a trigger payload could sensibly be. A body this size is a mistake at
    the sender, and it would be truncated into the prompt anyway.
    """
    token = _token(client, auth, project, db)
    oversized = "x" * (triggers_router.MAX_INBOUND_BYTES + 1024)

    resp = client.post(
        f"{API}/inbound/{token}",
        content=oversized.encode(),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert "KB" in resp.json()["detail"]


def test_a_firing_still_writes_a_piece(client, auth, project, db):
    """End to end through the threadpool, not just the guards in front of it."""
    from app.models.content import Content

    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    token = _token(client, auth, project, db, headline_path="title")

    resp = client.post(f"{API}/inbound/{token}", json={"title": "We shipped a thing"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "generated"
    assert db.query(Content).count() == 1
