"""The documentation half of the API contract, enforced.

Ninety-one endpoints were documented by hand over six commits. Nothing stopped
the ninety-second from arriving with none of it, and nothing would have said so
— a missing ``summary`` is not a failure, it is FastAPI quietly inventing one
from the function name, which is why "38 of 91 documented" was invisible for as
long as it was.

So the rules are asserted rather than remembered. Each test below is one rule,
and each failure names the endpoint and what to add:

* an explicit ``summary=`` — the derived one is not documentation;
* a docstring, which is where the ``description`` comes from;
* at least one failure declared, because an endpoint that only describes
  success describes half of itself;
* every declared 4xx/5xx using :class:`app.schemas.errors.ErrorOut`, so a
  generated client gets one error type and not a union of anonymous shapes —
  with one named exemption, below, for the liveness probes;
* a 401 and a 413 on anything behind ``get_current_user``;
* a 429 on anything the limiter touches.

The last two are derived from the endpoint's own wiring rather than from a
list kept here, so they cannot go stale: add ``@limiter.limit`` and the schema
requirement follows the decorator.

``include_in_schema=False`` routes are exempt throughout — they are not in the
schema, so there is nothing to document. ``/projects/{id}/feed.xml`` is the
only one, and it serves RSS to feed readers rather than JSON to clients.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.deps import get_current_user
from app.main import create_app
from app.ratelimit import limiter
from app.schemas.errors import ErrorOut

#: Codes FastAPI generates on its own, with its own model. A route is never
#: required to declare these and is never judged on how it declares them.
_FASTAPI_OWNED = {422}

#: ``(handler name, status code)`` allowed to answer with something other than
#: :class:`ErrorOut`. Both entries are the liveness probes, whose 503 is the
#: health body rather than an error: Caddy reads the status code and a human
#: reads the payload to find out *which* dependency is down. Answering
#: ``{"detail": ...}`` there would throw away the only useful part.
#:
#: Deliberately keyed by handler rather than by code, so this is an exemption
#: for two known endpoints and not a licence for the next 503.
_NON_ERROR_FAILURE_BODIES = {("health", 503), ("health_detail", 503)}


def _walk(routes):
    """Every :class:`APIRoute` under *routes*, however deeply nested.

    Recursive because FastAPI groups an ``include_router`` call behind a single
    ``_IncludedRouter`` entry on ``app.routes``: the endpoints are one level
    down from where they look like they are, and a non-recursive walk finds
    exactly one route in this app — the root — and reports everything as fine.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        yield from _walk(getattr(included, "routes", None) or getattr(route, "routes", []))


def _documented_routes() -> list[APIRoute]:
    """Every route of the real app that reaches the OpenAPI schema."""
    return [route for route in _walk(create_app().routes) if route.include_in_schema]


def _label(route: APIRoute) -> str:
    methods = ",".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
    return f"{methods} {route.path} ({route.endpoint.__name__})"


ROUTES = _documented_routes()

#: Parametrised by label so a failure names the endpoint in the test id rather
#: than only in the assertion message.
ROUTE_CASES = [pytest.param(route, id=_label(route)) for route in ROUTES]


def _declared_failures(route: APIRoute) -> dict[int, dict]:
    return {
        code: spec
        for code, spec in route.responses.items()
        if isinstance(code, int) and code >= 400 and code not in _FASTAPI_OWNED
    }


def _dependency_calls(route: APIRoute) -> set:
    """Every dependency callable on *route*, including nested ones."""
    seen = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            seen.add(dependant.call)
        stack.extend(dependant.dependencies)
    return seen


def _is_rate_limited(route: APIRoute) -> bool:
    """Whether slowapi holds a limit for this endpoint.

    ``Limiter`` registers under ``module.qualname-less-name`` at decoration
    time; ``functools.wraps`` keeps both attributes intact on the wrapper the
    router ends up holding, so the key can be rebuilt from the route.
    """
    key = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
    return bool(limiter._route_limits.get(key) or limiter._dynamic_route_limits.get(key))


def test_the_app_has_the_endpoints_this_module_thinks_it_has():
    """A guard on the walk itself.

    Every rule below is parametrised over ``ROUTES``. A walk that silently
    returned nothing — a FastAPI release that nests routers one level deeper,
    say — would turn this whole file into a suite of zero tests that passes.
    """
    assert len(ROUTES) > 80, f"only found {len(ROUTES)} routes; the walk is wrong"


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_every_endpoint_has_an_explicit_summary(route: APIRoute):
    """FastAPI derives one from the function name when this is missing.

    That derived summary is why an undocumented endpoint looks documented in
    ``/docs``: ``list_content`` becomes "List Content", which reads like a
    decision somebody made. Only an explicit ``summary=`` counts.
    """
    assert route.summary, (
        f"{_label(route)} has no summary. Add `summary=\"...\"` to the route "
        f"decorator — a few words in the imperative, as on its neighbours."
    )


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_every_endpoint_has_a_description(route: APIRoute):
    """The handler's docstring. Says what the endpoint does and why."""
    assert (route.description or "").strip(), (
        f"{_label(route)} has no docstring, so the schema has no description "
        f"for it."
    )


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_every_endpoint_declares_at_least_one_failure(route: APIRoute):
    """An endpoint that documents only success documents half of itself.

    The 422 FastAPI adds for body and parameter validation does not count: it
    is generated from the signature and says nothing about what this endpoint
    in particular refuses.
    """
    assert _declared_failures(route), (
        f"{_label(route)} declares no error responses. Compose them from "
        f"`app.schemas.errors`: `responses=errors(*AUTHENTICATED)` for a "
        f"plain authenticated endpoint, `errors(*OWNED)` when it addresses a "
        f"row by id, plus whatever else it can actually raise."
    )


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_every_declared_failure_uses_the_shared_error_model(route: APIRoute):
    """One error type across the API, so a generated client has one to handle.

    ``HTTPException`` renders ``{"detail": "..."}`` and the unhandled-exception
    handler matches it deliberately; a route that documented some other shape
    for a 4xx would be documenting something it cannot produce.
    """
    for code, spec in _declared_failures(route).items():
        if (route.endpoint.__name__, code) in _NON_ERROR_FAILURE_BODIES:
            continue
        assert spec.get("model") is ErrorOut, (
            f"{_label(route)} documents {code} with {spec.get('model')!r} "
            f"rather than ErrorOut. Use `errors({code})`, or pass "
            f'`{{"model": ErrorOut, "description": ...}}` if this code means '
            f"something the shared catalogue does not cover."
        )


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_authenticated_endpoints_declare_401_and_413(route: APIRoute):
    """Derived from the dependency, not from a list kept here.

    Anything behind ``get_current_user`` can be called without a token, and
    anything at all can be called with a body over the middleware's limit.
    Both are refusals a client has to handle and neither is visible from the
    success model.
    """
    if get_current_user not in _dependency_calls(route):
        return
    declared = set(_declared_failures(route))
    assert {401, 413} <= declared, (
        f"{_label(route)} is authenticated but declares {sorted(declared)}. "
        f"Every such endpoint starts from `errors(*AUTHENTICATED)`."
    )


@pytest.mark.parametrize("route", ROUTE_CASES)
def test_rate_limited_endpoints_declare_429(route: APIRoute):
    """If the limiter can refuse it, the schema has to say so.

    This is the rule that found the gaps: eight endpoints carried
    ``@limiter.limit`` and documented every failure except the one the
    decorator itself introduces.
    """
    if not _is_rate_limited(route):
        return
    assert 429 in _declared_failures(route), (
        f"{_label(route)} is rate-limited but does not declare 429. Add "
        f"`status.HTTP_429_TOO_MANY_REQUESTS` to its `errors(...)`."
    )


# --------------------------------------------------------------------------- #
# The rules, applied to an endpoint that breaks them.                          #
# --------------------------------------------------------------------------- #
#
# Everything above passes today, which is exactly the state a check that had
# quietly stopped working would also be in. These build the ninety-second
# endpoint — the undocumented one this file exists to catch — and assert that
# each rule refuses it.


@pytest.fixture
def undocumented_route() -> APIRoute:
    """A route with nothing on it: no summary, no docstring, no responses."""
    from fastapi import APIRouter, Depends, FastAPI

    router = APIRouter()

    @router.get("/new-thing")
    def new_thing(user=Depends(get_current_user)) -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    return next(r for r in _walk(app.routes) if r.path == "/new-thing")


def test_a_bare_endpoint_fails_the_summary_rule(undocumented_route):
    """FastAPI's derived summary is what makes this worth asserting.

    ``new_thing`` is presented as "New Thing" in ``/docs``, which is why a
    reader cannot tell an undocumented endpoint from a documented one and why
    the rule checks ``route.summary`` rather than the generated schema.
    """
    assert not undocumented_route.summary
    assert undocumented_route.path in _label(undocumented_route)


def test_a_bare_endpoint_fails_the_description_rule(undocumented_route):
    assert not (undocumented_route.description or "").strip()


def test_a_bare_endpoint_fails_the_failure_rule(undocumented_route):
    assert not _declared_failures(undocumented_route)


def test_a_bare_authenticated_endpoint_fails_the_401_rule(undocumented_route):
    """And the dependency walk finds the guard through ``Depends``."""
    assert get_current_user in _dependency_calls(undocumented_route)
    assert not {401, 413} <= set(_declared_failures(undocumented_route))


def test_a_limited_endpoint_is_recognised_as_limited():
    """The 429 rule reads slowapi's registry, which is an internal.

    If a slowapi upgrade moved or renamed ``_route_limits``, every endpoint
    would read as unlimited and the rule would pass by doing nothing. This
    registers a limit the same way the routers do and asserts it is seen.
    """
    from fastapi import APIRouter, FastAPI, Request, Response

    router = APIRouter()

    @router.get("/limited-thing")
    @limiter.limit("1/minute")
    def limited_thing(request: Request, response: Response) -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    route = next(r for r in _walk(app.routes) if r.path == "/limited-thing")

    assert _is_rate_limited(route)
    assert 429 not in _declared_failures(route)
