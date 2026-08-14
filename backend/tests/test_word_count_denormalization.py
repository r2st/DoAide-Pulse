"""``Content.word_count`` is stored, so it has to be kept true.

Denormalized data buys reads at the cost of a promise: the column says the same
thing as the body it came from, on every write path there is and after the
backfill that filled it in for rows that predate it. Break that promise and the
listing shows a length the editor disagrees with, the digest bills the wrong
reading time, and nothing crashes to say so.

Three kinds of assertion here, in that order:

* the sync itself — every shape of write, and the whitespace cases where a
  looser definition of "word" would drift from ``str.split``;
* the reason it exists — no listing endpoint selects an article body any more;
* the one hole the ORM-level sync cannot cover, pinned against the source so it
  stays hypothetical.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.models.content import (
    Content,
    ContentStatus,
    ContentType,
    read_minutes_for,
    word_count_of,
)
from app.models.project import Project, Tone


def _content(db, project, body: str = "") -> Content:
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.DRAFT,
        title="A piece",
        slug=f"a-piece-{body[:8]!r}",
        body_markdown=body,
    )
    db.add(content)
    db.commit()
    return content


# --------------------------------------------------------------------------- #
# The sync                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body, expected",
    [
        ("", 0),
        ("   ", 0),
        ("\n\n", 0),
        ("one", 1),
        ("one two three", 3),
        # The cases a "count the spaces" definition gets wrong, which is why the
        # backfill counts in Python rather than in SQL.
        ("one  two", 2),
        ("one\ttwo", 2),
        ("one\ntwo\nthree", 3),
        (" leading and trailing ", 3),
        # Markdown markers are words, deliberately: the count is a rough length,
        # and the frontend's own estimate (lib/editorStats.js) matches this one.
        ("# Heading\n\n```py\nx = 1\n```", 7),
    ],
)
def test_the_stored_count_is_str_split(db, project, body, expected):
    content = _content(db, project, body)

    assert content.word_count == expected == word_count_of(body)


def test_a_body_written_after_construction_recounts(db, project):
    content = _content(db, project, "one two")
    assert content.word_count == 2

    content.body_markdown = "one two three four"
    db.commit()

    assert content.word_count == 4


def test_a_body_set_to_none_counts_as_empty(db, project):
    """``nullable=False`` is the database's rule, not the attribute's.

    Assigning ``None`` in Python reaches the validator before it reaches the
    ``NOT NULL``, and counting ``None.split()`` would raise ``AttributeError``
    from inside an ORM event — a 500 where the write deserves the database's
    own complaint at flush. The validator absorbs it and leaves the flush to
    fail on its own terms.
    """
    content = _content(db, project, "one two")

    content.body_markdown = None

    assert content.word_count == 0


def test_a_piece_with_no_body_at_all_counts_zero(db, project):
    """``body_markdown`` defaults to ``""`` at flush, never visiting the setter.

    So the column default has to agree with what the validator would have
    computed. It does — both are zero — but they are two different mechanisms
    and only a test says they line up.
    """
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.DRAFT,
        title="Empty",
        slug="empty",
    )
    db.add(content)
    db.commit()
    db.expire_all()

    assert content.word_count == 0
    assert content.read_minutes == 1


def test_the_count_survives_a_round_trip_through_the_database(db, project):
    """The validator must not fire on load and recount from a deferred body.

    ``validates`` listens for ``set`` events only, so a row read back keeps the
    stored number. Worth pinning: if it ever fired on load, the endpoints that
    defer ``body_markdown`` would recount from an unloaded attribute and take a
    SELECT per row to do it — the exact cost the column removes.
    """
    content = _content(db, project, "word " * 400)
    content_id = content.id
    db.expire_all()

    reloaded = db.get(Content, content_id)

    assert reloaded.word_count == 400
    assert reloaded.read_minutes == read_minutes_for(400) == 2


def test_the_api_keeps_the_count_in_step_with_an_edit(client, auth, db, user):
    project = Project(
        user_id=user.id,
        name="Shipping",
        slug="shipping",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(project)
    db.commit()

    created = client.post(
        "/api/v1/content",
        json={
            "project_id": project.id,
            "content_type": "announcement",
            "title": "First",
            "body_markdown": "one two three",
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    assert created.json()["word_count"] == 3

    patched = client.patch(
        f"/api/v1/content/{created.json()['id']}",
        json={"body_markdown": "one two three four five"},
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["word_count"] == 5

    listed = client.get("/api/v1/content", headers=auth)
    assert [row["word_count"] for row in listed.json()] == [5]


# --------------------------------------------------------------------------- #
# The reason it exists                                                         #
# --------------------------------------------------------------------------- #


def _seed(db, user_id: int, count: int) -> None:
    for i in range(count):
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
                status=ContentStatus.REVIEW,
                title=f"Post {i}",
                slug=f"post-{i}",
                body_markdown="word " * 500,
            )
        )
    db.commit()
    db.expire_all()


@pytest.mark.parametrize("path", ["/api/v1/content", "/api/v1/content/queue/review"])
def test_a_listing_ships_no_article_bodies(client, auth, db, user, sql_log, path):
    """The whole point, pinned on the emitted SQL.

    A query count cannot see this — it was one statement before the ``defer``
    and it is one statement after. What changed is the width of the rows, and
    ``limit`` goes to 500 on the first of these paths.
    """
    _seed(db, user.id, 3)
    sql_log.clear()

    resp = client.get(path, headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["word_count"] for row in resp.json()] == [500, 500, 500]
    assert [row["read_minutes"] for row in resp.json()] == [2, 2, 2]
    carrying = [s for s in sql_log if "body_markdown" in s]
    assert carrying == [], "\n".join(s[:300] for s in carrying)


def test_the_detail_endpoint_still_returns_the_body(client, auth, db, user):
    """``defer`` is on the listings only — the editor needs the text."""
    _seed(db, user.id, 1)
    content_id = client.get("/api/v1/content", headers=auth).json()[0]["id"]

    resp = client.get(f"/api/v1/content/{content_id}", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["body_markdown"] == "word " * 500
    assert resp.json()["word_count"] == 500


# --------------------------------------------------------------------------- #
# The hole                                                                     #
# --------------------------------------------------------------------------- #


def test_nothing_writes_a_body_behind_the_orm():
    """A bulk ``UPDATE`` never visits an instance, so the validator never fires.

    That is the one write shape ``Content._sync_word_count`` cannot cover, and
    a caller reaching for it would leave the count stale with no error anywhere.
    Nothing does today. This fails the moment something starts, which is the
    point at which the count needs setting in the same statement.

    Matched on the source rather than at runtime because the failure has no
    symptom to observe: the row is simply wrong afterwards.

    Over the parsed tree rather than the text: the rule is written down in
    ``Content._sync_word_count``'s own docstring, and a grep for the pattern
    finds the prose describing it as readily as it would find the thing itself.
    """
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"

    def sets_body_in_bulk(tree: ast.AST) -> bool:
        # ``<anything>.values(body_markdown=...)`` — the shape an UPDATE takes,
        # whether it is spelled ``update(Content)`` or built up in pieces.
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
            and any(kw.arg == "body_markdown" for kw in node.keywords)
            for node in ast.walk(tree)
        )

    offenders = [
        path.relative_to(app_dir).as_posix()
        for path in sorted(app_dir.rglob("*.py"))
        if sets_body_in_bulk(ast.parse(path.read_text()))
    ]

    assert offenders == [], (
        "these write body_markdown without going through the ORM instance, so "
        "word_count is left stale: " + ", ".join(offenders)
    )
