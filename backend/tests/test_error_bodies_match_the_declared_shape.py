"""What the API actually sends when it refuses, across the whole surface.

``test_every_endpoint_is_documented`` and ``test_openapi_contract`` assert the
*declared* error contract: every endpoint names the codes it can produce, and
every one of them is declared as :class:`~app.schemas.errors.ErrorOut`. Both
read route objects. Neither sends a request, so neither can tell whether the
body that comes back is the body that was promised.

That gap is not hypothetical. ``ErrorOut`` is a documentation-only model here —
FastAPI validates *response* models, and an error is raised rather than
returned, so nothing at runtime forces an ``HTTPException`` detail to be the
string the schema says it is. A route that raised one with a dict, a list, or a
pydantic object would serialise it happily, ship a body no generated client can
read, and keep every existing test green: the schema still says ``{"detail":
string}`` because the schema is written by hand.

So this file makes the requests. Two sweeps, both parametrised over the real
route table rather than a list kept here:

* every endpoint behind ``get_current_user``, called with no token at all;
* every endpoint behind ``get_current_user`` that addresses a row by id, called
  with a valid token and an id nobody owns.

The second sweep accepts any 4xx, not only 404. A POST with a required body is
rejected by validation before ownership is ever checked, and that 422 is
FastAPI's own — a list of per-field objects, documented by FastAPI and
deliberately exempt below. What is asserted for every other code is the one
rule the whole surface is supposed to keep: a single ``detail`` key, holding a
non-empty string, and nothing else alongside it.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.deps import get_current_user
from app.main import create_app

#: FastAPI generates this one, with its own list-of-objects body, and documents
#: it itself. The only code exempt from the single-string rule.
_FASTAPI_OWNED = 422

#: An id no fixture creates. Large enough to be obviously synthetic and to stay
#: out of the way of any autoincrement a test does reach.
_NO_SUCH_ID = 987654321


def _walk(routes):
    """Every :class:`APIRoute` under *routes*, however deeply nested.

    ``include_router`` wraps each router rather than flattening it, so a
    non-recursive walk over ``app.routes`` finds exactly one route — the root —
    and reports the whole surface as fine.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        yield from _walk(getattr(inner, "routes", None) or getattr(route, "routes", []))


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


def _method(route: APIRoute) -> str:
    return sorted(route.methods - {"HEAD", "OPTIONS"})[0]


def _url(route: APIRoute, prefix: str = "/api/v1") -> str:
    """The route's path with every ``{param}`` filled with a nonexistent id."""
    url = route.path
    for name in route.param_convertors:
        url = url.replace("{" + name + "}", str(_NO_SUCH_ID))
    return prefix + url


AUTHENTICATED = [
    pytest.param(route, id=f"{_method(route)} {route.path}")
    for route in _walk(create_app().routes)
    if route.include_in_schema and get_current_user in _dependency_calls(route)
]

#: The subset that addresses something by id. ``preview-links/{link_id}`` has
#: two, and both are filled in.
BY_ID = [param for param in AUTHENTICATED if param.values[0].param_convertors]


def test_the_two_sweeps_are_the_size_they_should_be():
    """A guard on the guards.

    Every assertion below is parametrised over a list this module builds. A
    walk that missed the nested routers would empty both lists and turn the
    file green while checking nothing — the standing failure mode of a test
    that generates its own cases. The only defence is knowing the number.
    """
    assert len(AUTHENTICATED) == 92
    assert len(BY_ID) == 53


def _assert_error_shape(resp, url: str) -> None:
    """The rule: exactly one key, ``detail``, holding a non-empty string."""
    body = resp.json()
    assert isinstance(body, dict), f"{url} answered {resp.status_code} with {body!r}"
    assert set(body) == {"detail"}, (
        f"{url} answered {resp.status_code} with keys {sorted(body)}; "
        "ErrorOut declares exactly one, and a client written against the "
        "schema will read only that one."
    )
    assert isinstance(body["detail"], str), (
        f"{url} answered {resp.status_code} with a {type(body['detail']).__name__} "
        "detail; the schema promises a string, and nothing at runtime enforces it."
    )
    assert body["detail"].strip(), f"{url} answered {resp.status_code} with an empty detail"


@pytest.mark.parametrize("route", AUTHENTICATED)
def test_an_unauthenticated_call_is_refused_in_the_declared_shape(client, route):
    """No token at all. The one failure every authenticated endpoint shares.

    Nothing runs before the dependency, so this reaches no handler and has no
    side effect — which is what makes it safe to fire at every write endpoint
    on the surface, including the destructive ones.
    """
    url = _url(route)
    resp = client.request(_method(route), url)

    assert resp.status_code == 401, (
        f"{_method(route)} {url} answered {resp.status_code} without a token, "
        "not 401"
    )
    _assert_error_shape(resp, url)


@pytest.mark.parametrize("route", BY_ID)
def test_an_id_nobody_owns_is_refused_in_the_declared_shape(client, auth, route):
    """A valid token and an id that does not exist.

    The status is not asserted beyond "a refusal": a POST whose body is
    required is rejected by validation before ownership is checked, and that
    422 is FastAPI's own shape. Every other code has to keep the rule.

    A 404 here also carries a second guarantee the catalogue spells out — it is
    the same answer for "no such row" and "somebody else's row", so that a 403
    cannot be used to confirm a row exists. That is asserted in
    ``test_tenant_isolation``; this checks the body it comes in.
    """
    url = _url(route)
    resp = client.request(_method(route), url, headers=auth)

    assert 400 <= resp.status_code < 500, (
        f"{_method(route)} {url} answered {resp.status_code} for an id that "
        "does not exist, which is neither a refusal nor a 5xx we would notice"
    )
    if resp.status_code == _FASTAPI_OWNED:
        # FastAPI's own body: a list of per-field problems. Documented by
        # FastAPI, exempt from the single-string rule, and still checked for
        # being the shape it claims rather than something else again.
        assert isinstance(resp.json()["detail"], list)
        return
    _assert_error_shape(resp, url)
