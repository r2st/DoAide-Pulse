"""Every paginated endpoint must bound ``limit`` at both ends.

``GET /content`` bounded only the top of the range. A negative ``limit`` reached
SQLAlchemy's ``.limit()`` verbatim, and the two databases Pulse runs on read it
differently: SQLite treats ``LIMIT -1`` as "no limit" and hands back the whole
table — past the 500 cap the parameter exists to enforce — while Postgres
refuses it and the request becomes a 500. Neither is what ``le=500`` was for.

The sweep at the bottom is the part worth keeping: a new paginated endpoint that
copies the old signature reintroduces this, and a test that names only
``/content`` would not notice.
"""
from __future__ import annotations

import pytest

from app.main import app
from app.models.content import Content, ContentStatus, ContentType


@pytest.fixture
def eight_pieces(db, project):
    for index in range(8):
        db.add(
            Content(
                project_id=project.id,
                title=f"Piece {index}",
                slug=f"piece-{index}",
                content_type=ContentType.ANNOUNCEMENT,
                status=ContentStatus.DRAFT,
                body_markdown="body",
            )
        )
    db.commit()


@pytest.mark.parametrize("limit", [-1, -500, 0])
def test_a_limit_below_one_is_refused(client, auth, eight_pieces, limit):
    resp = client.get(f"/api/v1/content?limit={limit}", headers=auth)
    assert resp.status_code == 422, resp.text


def test_a_negative_limit_does_not_dump_the_table(client, auth, eight_pieces):
    """The specific failure: ``LIMIT -1`` returning everything on SQLite.

    Asserted separately from the 422 above because this is the consequence that
    mattered — the cap silently not applying, on the endpoint whose whole
    purpose is to cap.
    """
    resp = client.get("/api/v1/content?limit=-1", headers=auth)
    assert resp.status_code == 422
    assert resp.json()["detail"], "the refusal should say which parameter was wrong"


def test_a_valid_limit_still_works(client, auth, eight_pieces):
    resp = client.get("/api/v1/content?limit=3", headers=auth)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3
    # The count header reports the whole matching set, not the page.
    assert resp.headers["X-Total-Count"] == "8"


def test_the_upper_bound_is_still_enforced(client, auth, eight_pieces):
    resp = client.get("/api/v1/content?limit=501", headers=auth)
    assert resp.status_code == 422, resp.text


def _limit_params():
    """Every ``limit`` query parameter declared across the API.

    Walks recursively: this FastAPI version keeps an included router as a single
    ``_IncludedRouter`` entry in ``app.routes``, holding its real routes on
    ``original_router``, rather than flattening them into the parent. A
    non-recursive pass sees one endpoint out of eighty.
    """
    found: list[tuple[str, object]] = []

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
            for param in dependant.query_params:
                if param.name == "limit":
                    found.append((route.path, param.field_info))

    walk(app.routes)
    return found


def test_every_limit_parameter_in_the_api_bounds_both_ends():
    """No paginated endpoint may cap only the top of its range.

    Enumerated off the live route table rather than a hand-written list, so an
    endpoint added later is covered without anyone remembering to add it here.
    """
    params = _limit_params()
    assert params, "expected to find limit parameters to check"

    unbounded = [
        path
        for path, field in params
        if not any(
            getattr(meta, "ge", None) is not None or getattr(meta, "gt", None) is not None
            for meta in field.metadata
        )
    ]
    assert unbounded == [], (
        f"these endpoints accept a limit below 1: {unbounded}. A negative limit "
        "means 'no limit' on SQLite and an error on Postgres."
    )
