"""Query parameters that name a row are bounded like path parameters.

``QueryRowId`` closes the same hole that ``RowId`` closed for path segments:
a Python ``int`` has no width, so a ``project_id`` query parameter of
``2147483648`` sailed past FastAPI and crashed on ``NumericValueOutOfRange``
inside the database driver. The fix is one annotation, but it had been missed
on every ``project_id`` filter, the ``against`` parameter in revisions, the
``publication_id`` field in ``ScheduleUpdate``, and the ``language`` path
parameter in translations (unbounded length before this change).

The sweep at the end derives the check from the OpenAPI schema, so an endpoint
added later with a bare ``int`` query parameter named ``project_id`` fails
here rather than in production.
"""
from __future__ import annotations

import pytest

from app.deps import ROW_ID_MAX
from app.main import app

_TOO_WIDE = ROW_ID_MAX + 1


# ------------------------------------------------------------------
# project_id query parameter on every listing that accepts one
# ------------------------------------------------------------------

_PROJECT_ID_ENDPOINTS = [
    ("get", "/api/v1/content"),
    ("get", "/api/v1/api-keys"),
    ("get", "/api/v1/templates"),
    ("get", "/api/v1/triggers"),
    ("get", "/api/v1/tags"),
    ("get", "/api/v1/calendar"),
]


@pytest.mark.parametrize("method,path", _PROJECT_ID_ENDPOINTS)
def test_project_id_overflow_is_422_not_500(client, auth, method, path):
    resp = client.request(
        method.upper(), path, params={"project_id": _TOO_WIDE}, headers=auth
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("method,path", _PROJECT_ID_ENDPOINTS)
def test_project_id_at_max_still_reaches_the_query(client, auth, method, path):
    resp = client.request(
        method.upper(), path, params={"project_id": ROW_ID_MAX}, headers=auth
    )
    assert resp.status_code in (200, 404), resp.text
    assert resp.status_code != 500, resp.text


# ------------------------------------------------------------------
# against query parameter on the revision diff endpoint
# ------------------------------------------------------------------

def test_revision_against_overflow_is_422(client, auth):
    resp = client.get(
        "/api/v1/content/1/revisions/1/diff",
        params={"against": _TOO_WIDE},
        headers=auth,
    )
    assert resp.status_code == 422, resp.text


def test_revision_against_at_max_still_reaches_the_lookup(client, auth):
    resp = client.get(
        "/api/v1/content/1/revisions/1/diff",
        params={"against": ROW_ID_MAX},
        headers=auth,
    )
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------------
# publication_id in ScheduleUpdate body (calendar reschedule)
# ------------------------------------------------------------------

def test_schedule_update_publication_id_overflow_is_422(client, auth):
    resp = client.patch(
        "/api/v1/calendar/content/1",
        json={"scheduled_for": "2099-01-01T09:00:00Z", "publication_id": _TOO_WIDE},
        headers=auth,
    )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------
# language path parameter length bound on translations
# ------------------------------------------------------------------

def test_translation_language_too_long_is_422(client, auth):
    long_lang = "x" * 17
    resp = client.get(
        f"/api/v1/content/1/translations/{long_lang}",
        headers=auth,
    )
    assert resp.status_code == 422, resp.text


def test_translation_delete_language_too_long_is_422(client, auth):
    long_lang = "x" * 17
    resp = client.delete(
        f"/api/v1/content/1/translations/{long_lang}",
        headers=auth,
    )
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------
# Schema sweep: every integer query param named project_id must be bounded
# ------------------------------------------------------------------

def test_every_project_id_query_param_declares_its_ceiling():
    """Derived from the OpenAPI schema so a new endpoint with a bare ``int``
    project_id query parameter fails here rather than in production.
    """
    unbounded = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "query":
                    continue
                if parameter["name"] != "project_id":
                    continue
                schema = parameter.get("schema", {})
                arms = schema.get("anyOf", [schema])
                if not any(arm.get("maximum") == ROW_ID_MAX for arm in arms):
                    unbounded.append(f"{method.upper()} {path}")
    assert not unbounded, (
        "project_id query parameters with no ceiling (annotate with "
        "app.deps.QueryRowId):\n" + "\n".join(unbounded)
    )
