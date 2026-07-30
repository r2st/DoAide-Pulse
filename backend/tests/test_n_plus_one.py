"""Query counts for the list endpoints.

Every row in a content list carries its project's name, and the join in these
queries only *filters* — it does not populate ``Content.project``. Without an
eager load the relationship lazy-loads once per row, so the query count grows
with the result set: 500 extra round trips at the endpoint's ``limit`` ceiling.

The assertions are that the count does not change between a small and a large
result set. That is the property that matters, and unlike an absolute number it
does not need editing every time an unrelated query is added.

Each piece of content gets its **own** project on purpose. Sharing one project
would hide the bug: SQLAlchemy's identity map would serve every row after the
first from memory, turning an N+1 into a single extra query.
"""
from __future__ import annotations

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone


def _seed(
    db, user_id: int, count: int, *, offset: int = 0, status=ContentStatus.DRAFT
) -> None:
    for i in range(offset, offset + count):
        project = Project(
            user_id=user_id,
            name=f"Project {i}",
            slug=f"project-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.ANNOUNCEMENT,
                status=status,
                title=f"Post {i}",
                slug=f"post-{i}",
                body_markdown="Body.",
            )
        )
    db.commit()
    # Expire everything so the endpoint does real loads instead of reading the
    # objects this function just put in the identity map.
    db.expire_all()


def _selects(sql_log: list[str], table: str) -> list[str]:
    return [s for s in sql_log if s.startswith("SELECT") and f"FROM {table}" in s]


def test_content_list_loads_projects_in_one_query(client, auth, db, user, sql_log):
    _seed(db, user.id, 12)
    sql_log.clear()

    resp = client.get("/api/v1/content", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 12
    assert all(row["project_name"] for row in body)

    # The projects row arrives joined onto the content query, so this endpoint
    # issues no standalone SELECT against projects at all.
    assert _selects(sql_log, "projects") == []


def test_content_list_query_count_is_flat(client, auth, db, user, sql_log):
    user_id = user.id

    _seed(db, user_id, 3)
    sql_log.clear()
    assert len(client.get("/api/v1/content", headers=auth).json()) == 3
    few = len([s for s in sql_log if s.startswith("SELECT")])

    _seed(db, user_id, 30, offset=3)
    sql_log.clear()
    assert len(client.get("/api/v1/content", headers=auth).json()) == 33
    many = len([s for s in sql_log if s.startswith("SELECT")])

    assert few == many, f"{many - few} extra queries for 30 extra rows"


def test_review_queue_query_count_is_flat(client, auth, db, user, sql_log):
    user_id = user.id

    _seed(db, user_id, 3, status=ContentStatus.REVIEW)
    sql_log.clear()
    client.get("/api/v1/content/queue/review", headers=auth)
    few = len([s for s in sql_log if s.startswith("SELECT")])

    _seed(db, user_id, 20, offset=3, status=ContentStatus.REVIEW)
    sql_log.clear()
    resp = client.get("/api/v1/content/queue/review", headers=auth)
    assert len(resp.json()) == 23
    many = len([s for s in sql_log if s.startswith("SELECT")])

    assert few == many
    assert _selects(sql_log, "projects") == []


def test_dashboard_query_count_is_flat(client, auth, db, user, sql_log):
    """The dashboard has the same shape, and is the most-loaded page there is.

    It does query projects legitimately (the per-project aggregate), so the
    assertion is that the number does not move with the row count.
    """
    user_id = user.id

    _seed(db, user_id, 3)
    sql_log.clear()
    resp = client.get("/api/v1/analytics/dashboard", headers=auth)
    assert resp.status_code == 200, resp.text
    few = len([s for s in sql_log if s.startswith("SELECT")])

    # `recent_content` is capped at 8 rows, so 12 projects is past the ceiling:
    # a lazy load would add 8 queries here, an eager one none.
    _seed(db, user_id, 12, offset=3)
    sql_log.clear()
    resp = client.get("/api/v1/analytics/dashboard", headers=auth)
    assert len(resp.json()["recent_content"]) == 8
    many = len([s for s in sql_log if s.startswith("SELECT")])

    assert few == many, f"{many - few} extra queries for 12 extra rows"
