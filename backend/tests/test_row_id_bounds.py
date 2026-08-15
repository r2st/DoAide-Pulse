"""An id wider than the column that holds it is a 422, not a 500.

Every ``id`` in this tree is a SQLAlchemy ``Integer`` — PostgreSQL ``integer``,
signed 32-bit. A path signature saying ``int`` promises nothing of the sort: a
Python int has no width, so ``2147483648`` passed validation, went into the
query, and came back from the driver as ``NumericValueOutOfRange``. Every
by-id endpoint answered 500 to the integer immediately after the largest valid
one, and so did every paged listing to a large enough ``offset``.

Two halves, and the second is the one that lasts. The first pins the behaviour
on the endpoints that had it wrong. The second derives the check from the
OpenAPI schema, so an endpoint *added* later with a bare ``int`` id fails here
rather than in production — this class of bug is not one anybody re-finds by
hand, and the fix is only ever one annotation.
"""
from __future__ import annotations

import pytest

from app.deps import ROW_ID_MAX
from app.main import app

#: One past the widest id the database can be asked about.
_TOO_WIDE = ROW_ID_MAX + 1

#: A representative by-id endpoint from each router that has one.
_BY_ID = [
    ("get", "/api/v1/projects/{}"),
    ("patch", "/api/v1/projects/{}"),
    ("delete", "/api/v1/projects/{}"),
    ("post", "/api/v1/projects/{}/scan"),
    ("get", "/api/v1/projects/{}/ideas"),
    ("get", "/api/v1/content/{}"),
    ("patch", "/api/v1/content/{}"),
    ("delete", "/api/v1/content/{}"),
    ("post", "/api/v1/content/{}/approve"),
    ("get", "/api/v1/content/{}/links"),
    ("get", "/api/v1/content/{}/social"),
    ("get", "/api/v1/content/{}/schedule/suggestions"),
    ("get", "/api/v1/templates/{}"),
    ("delete", "/api/v1/templates/{}"),
    ("get", "/api/v1/triggers/{}/events"),
    ("delete", "/api/v1/triggers/{}"),
    ("delete", "/api/v1/webhooks/{}"),
    ("get", "/api/v1/webhooks/{}/deliveries"),
    ("get", "/api/v1/analytics/velocity/{}"),
]


def _call(client, auth, method: str, url: str):
    """One request, with an empty body where the verb needs one.

    PATCH here takes a required payload, and a bodyless PATCH is a 422 about
    the *body* — which would pass the too-wide assertion below for entirely the
    wrong reason and fail the valid-id one. ``{}`` is a legal no-op payload for
    both patched resources (every field is optional), so what either assertion
    then sees is the id.
    """
    kwargs = {"headers": auth}
    if method in {"patch", "post", "put"}:
        kwargs["json"] = {}
    return client.request(method.upper(), url, **kwargs)


@pytest.mark.parametrize("method,template", _BY_ID)
def test_an_id_wider_than_the_column_is_refused_not_crashed(
    client, auth, method, template
):
    resp = _call(client, auth, method, template.format(_TOO_WIDE))
    assert resp.status_code == 422, resp.text
    assert any(
        "less than or equal to" in (err.get("msg") or "")
        for err in resp.json()["detail"]
    ), resp.text


@pytest.mark.parametrize("method,template", _BY_ID)
def test_the_widest_valid_id_still_reaches_the_lookup(client, auth, method, template):
    """``ROW_ID_MAX`` itself is a real id the database can hold.

    The bound is inclusive, so this must reach the query and come back as an
    ordinary miss. A 422 here would mean the fence was posted one short and the
    largest usable id in the tree had become unaddressable.
    """
    resp = _call(client, auth, method, template.format(ROW_ID_MAX))
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("absent", [0, -1, 999999])
def test_an_absent_id_still_404s_rather_than_422ing(client, auth, absent):
    """Only an upper bound was added, deliberately.

    Zero and negatives cannot be primary keys, but they are *absent* ids rather
    than unaskable ones, and the 404 they already got is what keeps "no such
    project" and "not your project" the same answer. A ``ge=1`` here would tell
    an enumerating caller which ids are structurally possible.
    """
    resp = client.get(f"/api/v1/projects/{absent}", headers=auth)
    assert resp.status_code == 404, resp.text


def test_the_public_feed_is_bounded_too(client):
    """The one that matters most, and the one the schema check cannot see.

    ``/projects/{id}/feed.xml`` is ``include_in_schema=False``, so it is absent
    from the generated document the sweep above reads — and it is the only
    by-id endpoint here that takes no bearer token at all. An unauthenticated
    500 on a walkable integer is a different thing from an authenticated one.
    """
    resp = client.get(f"/api/v1/projects/{_TOO_WIDE}/feed.xml")
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/projects",
        "/api/v1/content",
        "/api/v1/content/queue/review",
        "/api/v1/content/queue/publications",
        "/api/v1/templates",
        "/api/v1/triggers",
        "/api/v1/webhooks",
    ],
)
def test_an_offset_past_the_bound_is_refused_not_crashed(client, auth, path):
    resp = client.get(path, params={"offset": _TOO_WIDE}, headers=auth)
    assert resp.status_code == 422, resp.text

    ok = client.get(path, params={"offset": ROW_ID_MAX}, headers=auth)
    assert ok.status_code == 200, ok.text


def test_every_integer_path_param_declares_its_ceiling():
    """The half that catches the endpoint nobody thought to add to the list.

    Derived from the generated schema rather than from the router source: the
    schema is what a client is promised, and a bound that is enforced but
    undocumented is the same surprise one request later.
    """
    unbounded = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "path":
                    continue
                schema = parameter.get("schema", {})
                if schema.get("type") != "integer":
                    continue
                if schema.get("maximum") != ROW_ID_MAX:
                    unbounded.append(f"{method.upper()} {path} — {parameter['name']}")
    assert not unbounded, (
        "integer path parameters with no ceiling (annotate with "
        "app.deps.RowId):\n" + "\n".join(unbounded)
    )


def test_every_paged_listing_bounds_its_offset():
    """The same argument as above, for the query half."""
    unbounded = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "query" or parameter["name"] != "offset":
                    continue
                schema = parameter.get("schema", {})
                # Optional query params are serialised as an anyOf; the bound
                # lives on the integer arm when they are.
                arms = schema.get("anyOf", [schema])
                if not any(arm.get("maximum") == ROW_ID_MAX for arm in arms):
                    unbounded.append(f"{method.upper()} {path}")
    assert not unbounded, (
        "paged listings with an unbounded offset (annotate with "
        "app.deps.ListOffset):\n" + "\n".join(unbounded)
    )
