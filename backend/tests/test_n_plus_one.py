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

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import AutopilotMode, Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.tasks import publish_tasks


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


def _seed_approved(db, user_id: int, count: int, *, offset: int = 0) -> None:
    """Approved pieces the backstop sweep will find, each on its own project.

    The projects are on ``auto`` with **no** destinations, so
    ``release_approved`` walks every relationship it needs and then returns
    empty at the destination check. That keeps the sweep's own inserts out of
    the query log, leaving only the loads this test is about.
    """
    for i in range(offset, offset + count):
        project = Project(
            user_id=user_id,
            name=f"Auto {i}",
            slug=f"auto-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
            autopilot_mode=AutopilotMode.AUTO,
            autopilot_platforms=[],
        )
        db.add(project)
        db.flush()
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.ANNOUNCEMENT,
                status=ContentStatus.APPROVED,
                title=f"Approved {i}",
                slug=f"approved-{i}",
                body_markdown="Body.",
            )
        )
    db.commit()
    db.expire_all()


def test_the_approved_sweep_loads_projects_in_one_query(db, user, monkeypatch, sql_log):
    """The beat sweep re-reads each piece's project, and used to do it per row.

    ``release_approved`` re-checks the project and its owner — the same two
    tables the sweep's own WHERE clause already joins against. Lazily that is
    two extra SELECTs per stuck piece on a task that runs every five minutes.
    """
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    _seed_approved(db, user.id, 15)
    sql_log.clear()

    result = publish_tasks.release_approved_content()

    assert result == {"found": 15, "released": 0}
    # The projects (and their owner) ride along on the query that finds the
    # content, so neither table is selected on its own.
    assert _selects(sql_log, "projects") == []
    assert _selects(sql_log, "users") == []


def test_the_approved_sweep_query_count_is_flat(db, user, monkeypatch, sql_log):
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    user_id = user.id

    _seed_approved(db, user_id, 3)
    sql_log.clear()
    assert publish_tasks.release_approved_content()["found"] == 3
    few = len([s for s in sql_log if s.startswith("SELECT")])

    _seed_approved(db, user_id, 30, offset=3)
    sql_log.clear()
    assert publish_tasks.release_approved_content()["found"] == 33
    many = len([s for s in sql_log if s.startswith("SELECT")])

    assert few == many, f"{many - few} extra queries for 30 extra pieces"


# --------------------------------------------------------------------------- #
# The bulk endpoints                                                           #
# --------------------------------------------------------------------------- #
#
# These take a list of ids — up to 100 — and used to `db.get` each one and then
# reach through `content.project` for the ownership check. Both are round
# trips, so the cost was 2n rather than the one query the whole batch needs.
# Unlike the list endpoints there is no pagination ceiling to hide behind: the
# id list is exactly as long as the user's selection.


def _seed_ids(
    db, user_id: int, count: int, *, offset: int = 0, status=ContentStatus.REVIEW
) -> list[int]:
    """*count* pieces, each on its own project, returned as ids.

    Separate projects for the same reason as ``_seed`` above: sharing one would
    let the identity map answer every ownership check after the first and hide
    the lazy load entirely.
    """
    ids: list[int] = []
    for i in range(offset, offset + count):
        project = Project(
            user_id=user_id,
            name=f"Bulk {i}",
            slug=f"bulk-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        content = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            status=status,
            title=f"Bulk piece {i}",
            slug=f"bulk-piece-{i}",
            body_markdown="Body.",
        )
        db.add(content)
        db.flush()
        ids.append(content.id)
    db.commit()
    db.expire_all()
    return ids


def test_bulk_reject_query_count_is_flat(client, auth, db, user, sql_log):
    user_id = user.id

    few = _seed_ids(db, user_id, 2)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/reject", json={"content_ids": few}, headers=auth
    )
    assert resp.json()["succeeded"] == few
    small = len([s for s in sql_log if s.startswith("SELECT")])

    many = _seed_ids(db, user_id, 20, offset=100)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/reject", json={"content_ids": many}, headers=auth
    )
    assert resp.json()["succeeded"] == many
    large = len([s for s in sql_log if s.startswith("SELECT")])

    assert small == large, f"{large - small} extra queries for 18 extra ids"


def test_bulk_approve_query_count_is_flat(client, auth, db, user, sql_log):
    """Approve reads the rows twice: once to set the status, and again after
    the commit — which expired them — for ``release_approved`` to decide on.
    Both halves have to be one query, not one per piece.

    These projects are on the default (manual) autopilot mode, so the release
    bails at the mode check without committing. That is the point: it still
    walks ``publications``, ``project`` and ``project.user`` to get there.
    """
    user_id = user.id

    few = _seed_ids(db, user_id, 2)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/approve", json={"content_ids": few}, headers=auth
    )
    assert resp.json()["succeeded"] == few
    small = len([s for s in sql_log if s.startswith("SELECT")])

    many = _seed_ids(db, user_id, 20, offset=200)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/approve", json={"content_ids": many}, headers=auth
    )
    assert resp.json()["succeeded"] == many
    large = len([s for s in sql_log if s.startswith("SELECT")])

    assert small == large, f"{large - small} extra queries for 18 extra ids"
    assert _selects(sql_log, "projects") == []


def test_bulk_publish_does_not_walk_to_the_owner_per_piece(client, auth, db, user, sql_log):
    """``_queue_publish`` used to read ``content.project.user`` for the
    connection check — two lazy hops per piece on top of the lookup, making the
    publish batch the most expensive of the three. It takes the authenticated
    user directly now; both callers have already proven the piece is theirs.

    Nothing is connected here, so every item fails that check — which is
    exactly the path that used to do the walking.
    """
    user_id = user.id

    few = _seed_ids(db, user_id, 2, status=ContentStatus.APPROVED)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={"content_ids": few, "platforms": ["devto"]},
        headers=auth,
    )
    assert [f["content_id"] for f in resp.json()["failed"]] == few
    small = len([s for s in sql_log if s.startswith("SELECT")])

    many = _seed_ids(db, user_id, 20, offset=300, status=ContentStatus.APPROVED)
    sql_log.clear()
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={"content_ids": many, "platforms": ["devto"]},
        headers=auth,
    )
    assert [f["content_id"] for f in resp.json()["failed"]] == many
    large = len([s for s in sql_log if s.startswith("SELECT")])

    assert small == large, f"{large - small} extra queries for 18 extra ids"
    assert _selects(sql_log, "projects") == []
    # One, and only one: the bearer token's own user lookup. The owner behind
    # each piece rides along on the content query.
    assert len(_selects(sql_log, "users")) == 1


def test_a_bulk_batch_still_reports_ids_it_does_not_own(client, auth, db, user):
    """Ownership moved into the WHERE clause; "not mine" must still read as
    "not found" rather than quietly succeeding or 403-ing the whole batch.
    """
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()

    mine = _seed_ids(db, user.id, 1, offset=400)
    theirs = _seed_ids(db, stranger.id, 1, offset=500)

    resp = client.post(
        "/api/v1/content/bulk/reject",
        json={"content_ids": mine + theirs + [999_999]},
        headers=auth,
    )

    body = resp.json()
    assert body["succeeded"] == mine
    assert {f["content_id"] for f in body["failed"]} == {theirs[0], 999_999}
    assert {f["reason"] for f in body["failed"]} == {"Not found"}


def test_a_bulk_batch_keeps_the_order_it_was_given(client, auth, db, user):
    """The result is read back positionally by the review queue, and the fetch
    is now one unordered query — so the loop, not the database, has to own the
    order.
    """
    ids = _seed_ids(db, user.id, 4, offset=600)
    shuffled = [ids[2], ids[0], ids[3], ids[1]]

    resp = client.post(
        "/api/v1/content/bulk/reject", json={"content_ids": shuffled}, headers=auth
    )

    assert resp.json()["succeeded"] == shuffled


# --------------------------------------------------------------------------- #
# The projects list                                                            #
# --------------------------------------------------------------------------- #
#
# This endpoint carries two hand-written N+1 fixes — the `selectinload` of
# `Project.triggers` that `autopilot_blocked_reason` reads, and `_batch_counts`
# for the two content counts — each with a comment explaining the round trip it
# removes. Nothing failed if either were dropped. A fix whose only record is a
# comment is one refactor away from being reverted by accident, which is the
# same argument the rest of this file makes for the content list.


def _seed_projects(db, user_id: int, count: int, *, offset: int = 0) -> None:
    """Projects with a trigger and a piece of content each.

    Both are needed for the counts to be non-trivial, and a trigger per project
    is what makes a missing eager load show up as one SELECT per row.
    """
    from app.models.trigger import Trigger, TriggerKind

    for i in range(offset, offset + count):
        project = Project(
            user_id=user_id,
            name=f"Project {i:03d}",
            slug=f"project-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
            repo_url=f"https://github.com/acme/project-{i}",
            autopilot_mode=AutopilotMode.AUTO,
        )
        db.add(project)
        db.flush()
        db.add(
            Trigger(
                project_id=project.id,
                kind=TriggerKind.SCHEDULE,
                config={"cron": "0 9 * * 1"},
            )
        )
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.ANNOUNCEMENT,
                status=ContentStatus.PUBLISHED,
                title=f"Post {i}",
                slug=f"post-{i}",
                body_markdown="Body.",
            )
        )
    db.commit()


def test_the_projects_list_loads_triggers_in_one_query(client, auth, db, user, sql_log):
    """``autopilot_blocked_reason`` reads ``project.triggers`` for every row."""
    _seed_projects(db, user.id, 12)
    sql_log.clear()

    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 12

    # One `IN` load for the whole page, not one SELECT per project.
    assert len(_selects(sql_log, "triggers")) == 1


def test_the_projects_list_counts_content_in_one_query(client, auth, db, user, sql_log):
    """The two counts per row come from ``_batch_counts``, not per project."""
    _seed_projects(db, user.id, 12)
    sql_log.clear()

    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200
    assert [p["content_count"] for p in resp.json()] == [1] * 12
    assert [p["published_count"] for p in resp.json()] == [1] * 12

    assert len(_selects(sql_log, "content")) == 1


def test_the_projects_list_query_count_is_flat(client, auth, db, user, sql_log):
    """The property that survives unrelated queries being added.

    Four times the rows must not mean four times the round trips.
    """
    _seed_projects(db, user.id, 3)
    sql_log.clear()
    client.get("/api/v1/projects", headers=auth)
    small = len(sql_log)

    _seed_projects(db, user.id, 9, offset=3)
    sql_log.clear()
    resp = client.get("/api/v1/projects", headers=auth)
    assert len(resp.json()) == 12
    large = len(sql_log)

    assert small == large, f"{small} queries for 3 projects, {large} for 12"


def test_the_dashboard_does_not_carry_an_article_body(client, auth, db, user, sql_log):
    """Flatness is not narrowness — the counts above cannot see this.

    Two blocks on this page rendered a title and four scalars per row, and both
    reached them through a ``Content`` entity: the upcoming list via
    ``joinedload(Publication.content)``, the recent list by selecting ``Content``
    outright. That is up to thirteen whole article bodies, plus four JSON
    columns each, on the page every session opens first — and the query count is
    identical either way, so ``test_dashboard_query_count_is_flat`` passed
    throughout.
    """
    project = Project(
        user_id=user.id,
        name="Wide",
        slug="wide",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(project)
    db.flush()
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        title="A long one",
        slug="a-long-one",
        body_markdown="word " * 5000,
    )
    db.add(content)
    db.flush()
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db.commit()
    db.expire_all()

    sql_log.clear()
    resp = client.get("/api/v1/analytics/dashboard", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Both blocks still render, and still carry the title they needed the row for.
    assert [row["title"] for row in body["upcoming"]] == ["A long one"]
    assert [row["title"] for row in body["recent_content"]] == ["A long one"]
    assert body["recent_content"][0]["project_name"] == "Wide"

    bodies = [s for s in sql_log if "content.body_markdown" in s]
    assert bodies == [], "\n".join(s[:300] for s in bodies)
