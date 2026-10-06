"""Every endpoint that answers with an array has to say what bounds it.

``test_pagination_bounds`` sweeps the ``limit`` parameters the API declares and
insists each one caps both ends of its range. It cannot see the failure that
matters more: an endpoint with no ``limit`` at all. ``GET /templates`` had none,
and answered with five megabytes; ``GET /webhooks`` had none, and leaned on a
ceiling checked in ``create`` — a different endpoint, which is not a bound on
this one, since rows can predate a cap that was lowered.

So the rule here is a rule about *declaring*, not about paging. A collection
endpoint is fine unbounded when the collection itself is finite — one row per
:class:`Platform`, one per :class:`TriggerKind`, one per publication of a single
piece. What is not fine is nobody having decided which case an endpoint is in.
Each of those appears below with the thing that bounds it written down, and
anything else must declare a ``limit`` with ``ge`` and ``le`` on it.

The table is deliberately annoying to extend. A new list endpoint fails this
until someone writes a sentence about how big its answer can get, which is the
sentence that was missing both times.
"""
from __future__ import annotations

from typing import get_origin

from app.main import app

#: Endpoints whose answer is finite for a reason other than paging, and the
#: reason. The value is prose on purpose — it is the whole content of the test.
FINITE_BY_CONSTRUCTION = {
    ("GET", "/settings/platforms"): "one row per Platform, a closed enum",
    ("GET", "/triggers/kinds"): "one row per TriggerKind, a closed enum",
    ("GET", "/webhooks/events"): "one row per WebhookEvent, a closed enum",
    ("GET", "/api-keys/scopes"): "one row per ApiKeyScope, a closed enum",
    ("GET", "/templates/builtins"): "one row per BUILTINS entry, a module constant",
    ("GET", "/languages"): "one row per languages.LANGUAGES entry, a module constant",
    ("GET", "/calendar/cadence"): "one row per Platform, narrowed by ?platform",
    ("GET", "/analytics/engagement-trend"): "one point per day, bounded by ?days",
    ("GET", "/analytics/published"): (
        "one point per day, bounded by ?days — and fewer when ?weekly buckets "
        "them by seven"
    ),
("GET", "/analytics/platforms"): "one row per Platform, a closed enum",
    ("GET", "/content/{content_id}/headlines/performance"): (
        "one window per headline test on a single piece, and a piece holds one "
        "headline_history capped by the headline service"
    ),
    ("GET", "/content/{content_id}/schedule/suggestions"): (
        "one slot per requested platform, bounded by ?platforms"
    ),
    ("GET", "/projects/{project_id}/ideas"): "a literal .limit(12) in the query",
    # Everything below answers with the publications of exactly one piece, so
    # the ceiling is platforms-per-piece — one row per Platform at the very
    # most, and the same closed enum as the first entries here.
    ("POST", "/content/{content_id}/publish"): "the publications of one piece",
    ("POST", "/content/{content_id}/schedule"): "the publications of one piece",
    ("DELETE", "/content/{content_id}/schedule"): "the publications of one piece",
    ("PATCH", "/calendar/content/{content_id}"): "the publications of one piece",
}


def _collection_routes() -> list[tuple[str, str, dict]]:
    """``(method, path, {query param name: FieldInfo})`` for every list route.

    Walks recursively for the reason ``test_pagination_bounds._limit_params``
    gives: an included router is one entry in ``app.routes`` holding its real
    routes on ``original_router``.
    """
    found: list[tuple[str, str, dict]] = []

    def walk(routes) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            if get_origin(getattr(route, "response_model", None)) is not list:
                continue
            for method in sorted(route.methods):
                found.append(
                    (
                        method,
                        route.path,
                        {p.name: p.field_info for p in dependant.query_params},
                    )
                )

    walk(app.routes)
    return found


def _bounded_limit(field) -> bool:
    """A ``limit`` that caps both ends. ``ge``/``gt`` and ``le``/``lt``."""
    if field is None:
        return False
    metadata = list(field.metadata)
    low = any(getattr(m, "ge", None) is not None or getattr(m, "gt", None) is not None
              for m in metadata)
    high = any(getattr(m, "le", None) is not None or getattr(m, "lt", None) is not None
               for m in metadata)
    return low and high


def test_every_collection_endpoint_either_pages_or_says_why_it_need_not():
    routes = _collection_routes()
    assert routes, "expected to find list-returning routes to check"

    undeclared = [
        f"{method} {path}"
        for method, path, params in routes
        if (method, path) not in FINITE_BY_CONSTRUCTION
        and not _bounded_limit(params.get("limit"))
    ]
    assert undeclared == [], (
        "these endpoints answer with an array whose size nothing bounds: "
        f"{undeclared}. Either give them a `limit` with `ge` and `le`, or add "
        "them to FINITE_BY_CONSTRUCTION with the reason their answer is finite."
    )


def test_the_table_names_no_endpoint_that_has_since_gone_away():
    """A stale exemption is how the next unbounded listing gets waved through.

    The entries here are paths, and a path that no longer exists — renamed,
    deleted, or given a ``limit`` after all — is an exemption sitting ready to
    match something it was never reasoned about.
    """
    live = {(method, path) for method, path, _ in _collection_routes()}
    stale = sorted(f"{m} {p}" for m, p in FINITE_BY_CONSTRUCTION if (m, p) not in live)
    assert stale == [], f"FINITE_BY_CONSTRUCTION names routes that are gone: {stale}"


def test_the_two_that_prompted_this_are_paged_now():
    """Named rather than left to the sweep, since the sweep passes either way.

    ``FINITE_BY_CONSTRUCTION`` would satisfy it just as well as a ``limit``
    does, and for these two that would be the wrong answer — a template body is
    50,000 characters and the webhook ceiling lives on a different endpoint.
    """
    paged = {
        (method, path)
        for method, path, params in _collection_routes()
        if _bounded_limit(params.get("limit"))
    }
    assert ("GET", "/templates") in paged
    assert ("GET", "/webhooks") in paged
