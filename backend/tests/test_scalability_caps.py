"""Scalability caps: queries that scan a growing table carry a SQL LIMIT.

The internal-links endpoint loads published pieces in a project to score
keyword overlap in Python.  That is fine for a handful of posts and an
unbounded scan on a project with a thousand.  The candidate query now carries a
LIMIT so the work stays constant however long the project has been running.
"""
from __future__ import annotations

from sqlalchemy import event

from app.models.content import Content, ContentStatus


def test_internal_links_candidate_query_has_a_limit(db, client, auth, project):
    """The candidate SELECT carries a LIMIT, not just the returned list."""
    content = Content(
        project_id=project.id,
        title="Main Post",
        slug="main-post",
        body_markdown="# Main",
        content_type="tutorial",
        status=ContentStatus.PUBLISHED,
        keywords=["python", "fastapi"],
        focus_keyword="python",
    )
    db.add(content)
    db.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(str(statement).split())
        if "content" in flat.lower() and "slug" in flat.lower():
            statements.append(flat)

    from tests.conftest import engine

    event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = client.get(
            f"/api/v1/content/{content.id}/internal-links",
            headers=auth,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 200
    candidate_queries = [s for s in statements if "slug" in s.lower()]
    assert candidate_queries, "expected to capture the candidate query"
    assert any("LIMIT" in s.upper() for s in candidate_queries), (
        "the internal-links candidate query must carry a SQL LIMIT"
    )
