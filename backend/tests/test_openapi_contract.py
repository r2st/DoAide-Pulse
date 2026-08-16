"""The generated schema is the API's documentation. Nothing checked it.

Every documented route in this tree carries a ``summary``, a docstring FastAPI
turns into the operation's ``description``, and a ``responses=errors(...)`` map
naming the failures it can actually produce. That is a real standard and the
whole surface already meets it — ninety operations, no exceptions — but it was
held up by habit alone. Nothing failed when an endpoint shipped without it, and
the gap does not show in a test of the endpoint's behaviour, only in ``/docs``
months later, where a reader cannot tell an undocumented 404 from one that
cannot happen.

These assertions read the **route objects**, not the rendered schema, because
the rendered schema cannot answer the question. FastAPI fills a missing
``summary`` in with the function name title-cased, so ``list_events`` becomes
"List Events" and a presence check against ``openapi()`` passes for an endpoint
nobody documented — it was written that way first, and it stayed green when the
``summary=`` was deleted to test it. ``APIRoute.summary`` is ``None`` unless an
author wrote one, which is the actual question.

The failure this prevents is asymmetric. An endpoint with no summary is a blank
line in the reference; an endpoint with no declared errors tells a generated
client that the error branch is ``Any``, and tells a reader that a call which
can 409 cannot. Both are cheap on the day the route is written and nobody goes
back for them.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import create_app
from app.schemas.errors import errors


def _documented_routes(routes):
    """Every ``APIRoute`` that appears in the schema, however deeply nested.

    ``include_router`` wraps each router rather than flattening it, so the
    app's own ``routes`` list holds ten wrappers and one real route. Anything
    that walks only the top level checks almost nothing and looks like it
    checked everything.
    """
    for route in routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            yield route
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _documented_routes(inner.routes)


ROUTES = [
    pytest.param(route, id=f"{sorted(route.methods)[0]} {route.path}")
    for route in _documented_routes(create_app().routes)
]

# Success codes. 202 is a success too, and both endpoints that answer it are
# deliberate: a password reset and an inbound trigger each accept the request
# and answer before the work is done.
SUCCESS = {200, 201, 202, 204}

# The status codes described in an endpoint's own words rather than the
# catalogue's, listed so a fifth has to be added here on purpose. Each is a
# code that means something different on a *public* route than it does on the
# authenticated surface the catalogue is written for: an unauthenticated caller
# must not be able to tell "no such thing" from "revoked", and the health probe
# answers 503 with the same body as a healthy response rather than an error.
DELIBERATE_OVERRIDES = {
    ("GET", "/health", 503),
    ("GET", "/health/detail", 503),
    ("GET", "/content/preview/{token}", 404),
    ("POST", "/triggers/inbound/{token}", 404),
    ("POST", "/triggers/inbound/{token}", 401),
}


def test_the_surface_is_the_size_this_file_thinks_it_is():
    """A guard on the guard. Every assertion below is parametrised over the
    route list, so a bug that emptied it — or a walk that missed the nested
    routers, as the first version of it did — would turn this file green
    rather than red. That is the failure mode of a test generating its own
    cases, and the only defence is knowing the number."""
    assert len(ROUTES) == 123


@pytest.mark.parametrize("route", ROUTES)
def test_every_operation_has_a_summary_somebody_wrote(route):
    """``summary`` is the line ``/docs`` lists the endpoint by.

    ``route.summary`` rather than the schema's: the schema always has one,
    because FastAPI invents it from the function name.
    """
    assert route.summary, f"{sorted(route.methods)} {route.path} has no summary="


@pytest.mark.parametrize("route", ROUTES)
def test_every_operation_has_a_description(route):
    """The route's docstring. This is where the reason lives — which of two
    endpoints to reach for, and what the surprising cases do."""
    assert (route.description or "").strip(), (
        f"{sorted(route.methods)} {route.path} has no docstring"
    )


@pytest.mark.parametrize("route", ROUTES)
def test_every_operation_documents_at_least_one_failure(route):
    """FastAPI's automatic 422 does not count: it is generated for anything
    with a body and says nothing about this endpoint. An operation whose only
    documented outcomes are success and 422 has not declared its errors."""
    declared = {int(code) for code in route.responses} - SUCCESS - {422}
    assert declared, (
        f"{sorted(route.methods)} {route.path} documents no failure it can "
        "produce — pass responses=errors(...)"
    )


@pytest.mark.parametrize("route", ROUTES)
def test_error_descriptions_come_from_the_shared_catalogue(route):
    """One 404 should read the same as every other 404.

    ``schemas.errors`` exists so the wording is written once; an endpoint that
    spells its own out by hand drifts from the rest the first time the shared
    one is edited. The exceptions are enumerated above rather than allowed by
    the rule, so adding one is a decision somebody makes.
    """
    verb = sorted(route.methods)[0]
    for code, body in route.responses.items():
        code = int(code)
        if code in SUCCESS or (verb, route.path, code) in DELIBERATE_OVERRIDES:
            continue
        # ``errors`` raises for a code it has no wording for, which is the same
        # complaint this test makes — just with a worse message.
        try:
            expected = errors(code)[code]
        except KeyError:
            raise AssertionError(
                f"{verb} {route.path} declares {code}, which schemas.errors "
                "has no entry for — describe it there rather than here"
            ) from None
        assert body.get("description") == expected["description"], (
            f"{verb} {route.path} describes {code} in its own words; use "
            "errors() or add it to DELIBERATE_OVERRIDES with a reason"
        )


def test_every_deliberate_override_is_still_there():
    """An override left behind after its endpoint changed is a stale exemption:
    it exempts nothing, and it reads as though the rule has a hole in it."""
    live = {
        (sorted(route.methods)[0], route.path, int(code))
        for route in _documented_routes(create_app().routes)
        for code in route.responses
    }
    assert live >= DELIBERATE_OVERRIDES, DELIBERATE_OVERRIDES - live
