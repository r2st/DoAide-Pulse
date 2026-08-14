"""The idea-write guard has to hold against two clicks, not two requests.

:mod:`tests.test_idea_write_once` establishes that a second call to
``POST /content/ideas/{id}/write`` replays the first draft instead of writing
another. Every test there is sequential: the first request has fully committed
before the second one starts, so ``used_content_id`` is set by the time the
second reads it.

That is not the case the guard exists for. The docstring on the endpoint says
so outright — generation takes long enough that the button looks unresponsive,
and the click that follows lands *during* the first request, not after it. Two
requests in flight together both read ``used_content_id`` as ``NULL``, both
spend a model call, and both write. The guard is a check-then-write, and a
check-then-write over an unlocked row is not idempotent.

So the read takes the row lock. What can be asserted in-process is:

* **the lock is asked for** — on a dialect that has one, the statement carries
  ``FOR UPDATE``. SQLite has no such thing and SQLAlchemy emits nothing for it,
  which is why the suite could never have noticed its absence;
* **the locked read sees the committed value** rather than a cached instance,
  which is the entire point of taking it;
* **it locks before it generates**, since a lock taken after the model call
  serialises nothing that costs anything;
* **the sequential behaviour is unchanged** — a lock that alters the answer is
  a different bug.

The interleaving itself is not simulated. Doing so needs two connections and
two transactions against a database that implements row locks, which is a
PostgreSQL integration test; what is in reach here is that the statement the
endpoint sends is the one that would win that race.
"""
from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.content import Content, ContentIdea, ContentType
from app.services import content_generator
from app.services.content_generator import GeneratedContent


@pytest.fixture
def idea(db, project) -> ContentIdea:
    row = ContentIdea(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        headline="Retry logic that does not double-post",
        rationale="The retry story is the one people ask about.",
        source={"kind": "test"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def calls(monkeypatch) -> list[dict]:
    """Count generations, and make each one produce a distinct title."""
    seen: list[dict] = []

    def _generate(project, content_type, **kwargs):
        seen.append({"content_type": content_type, **kwargs})
        n = len(seen)
        return GeneratedContent(
            title=f"Generated {n}",
            body_markdown=f"Body {n}.\n\n" + ("word " * 200),
            excerpt=f"Excerpt {n}.",
            meta_description=f"Meta {n}.",
            keywords=["retries"],
            tags=["python"],
            focus_keyword="retries",
            confidence=0.5,
        )

    monkeypatch.setattr(content_generator, "generate", _generate)
    return seen


# ---- The lock is asked for ------------------------------------------------ #


def test_the_idea_is_read_for_update(client, auth, db, idea, calls, monkeypatch):
    """The statement, compiled for a dialect that has row locks.

    SQLite is the reason this needs saying at all: SQLAlchemy emits no ``FOR
    UPDATE`` for it, so the whole suite runs green whether the lock is there or
    not. Compiling the select the endpoint actually issued against the
    PostgreSQL dialect is what makes its absence visible.
    """
    compiled: list[str] = []
    original = Session.scalar

    def _record(self, statement, *args, **kwargs):
        # Not every statement a session issues is an ORM select the PostgreSQL
        # dialect will compile; the ones that are not simply cannot be the read
        # this test is looking for.
        with contextlib.suppress(Exception):
            compiled.append(str(statement.compile(dialect=postgresql.dialect())))
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", _record)

    resp = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert resp.status_code == 201, resp.text
    locked = [
        sql for sql in compiled if "content_ideas" in sql and "FOR UPDATE" in sql
    ]
    assert locked, (
        "the idea row is read without a lock; two clicks in flight together "
        f"will both generate. Statements seen: {compiled}"
    )


def test_the_lock_is_taken_before_the_model_is_called(
    client, auth, db, idea, monkeypatch
):
    """A lock taken after the generation serialises nothing worth serialising.

    The expensive half is the model call, and the point of the lock is that the
    second request waits *instead of* spending one.
    """
    order: list[str] = []
    original = Session.scalar

    def _record(self, statement, *args, **kwargs):
        with contextlib.suppress(Exception):
            if "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect())):
                order.append("lock")
        return original(self, statement, *args, **kwargs)

    def _generate(project, content_type, **kwargs):
        order.append("generate")
        return GeneratedContent(
            title="Generated",
            body_markdown="Body.\n\n" + ("word " * 200),
            excerpt="Excerpt.",
            meta_description="Meta.",
            keywords=["retries"],
            tags=["python"],
            focus_keyword="retries",
            confidence=0.5,
        )

    monkeypatch.setattr(Session, "scalar", _record)
    monkeypatch.setattr(content_generator, "generate", _generate)

    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert order.index("lock") < order.index("generate")


# ---- And it reads what the lock was for ----------------------------------- #


def test_the_locked_read_sees_a_value_written_since_the_session_last_looked(
    client, auth, db, idea, calls
):
    """The half a lock is useless without.

    A locked read answered from the identity map has taken the lock and then
    ignored what the row says — the winning request's ``used_content_id`` would
    be invisible to the waiter, which would generate anyway. Simulated here by
    loading the idea into the session's map, then writing the column behind its
    back, which is what the other request's commit looks like from here.
    """
    first = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    assert first.status_code == 201, first.text
    draft_id = first.json()["id"]

    # Put a stale copy in the map: used_content_id as it was before the write.
    stale = db.get(ContentIdea, idea.id)
    stale.used_content_id = None
    db.expire_all()
    db.execute(
        ContentIdea.__table__.update()
        .where(ContentIdea.id == idea.id)
        .values(used_content_id=draft_id)
    )
    db.commit()

    second = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == draft_id
    assert len(calls) == 1


# ---- Without changing any of the answers ---------------------------------- #


def test_the_first_write_still_creates_and_answers_201(client, auth, db, idea, calls):
    resp = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert resp.status_code == 201, resp.text
    assert len(calls) == 1
    db.refresh(idea)
    assert idea.used_content_id == resp.json()["id"]


def test_the_replay_still_answers_200_with_the_same_draft(
    client, auth, db, idea, calls
):
    first = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    second = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(calls) == 1
    assert db.query(Content).filter(Content.project_id == idea.project_id).count() == 1


def test_a_missing_idea_is_still_a_404_and_still_locks_nothing(client, auth, calls):
    """The lock is on a row that may not exist. ``scalar`` returns None for that
    exactly as ``db.get`` did, and the 404 has to come before the model call."""
    resp = client.post("/api/v1/content/ideas/999999/write", headers=auth)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Idea not found"
    assert not calls


def test_a_strangers_idea_is_still_a_404_after_the_lock(client, auth, db, calls, user):
    """Ownership is checked after the row is read, so a lock is briefly taken on
    a row belonging to someone else. It is released with the request's
    transaction and nothing is disclosed — the answer is the same 404 as for an
    id that does not exist."""
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    their_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(their_project)
    db.commit()
    their_idea = ContentIdea(
        project_id=their_project.id,
        content_type=ContentType.ANNOUNCEMENT,
        headline="Not yours either",
        rationale="",
        source={},
    )
    db.add(their_idea)
    db.commit()

    resp = client.post(f"/api/v1/content/ideas/{their_idea.id}/write", headers=auth)

    assert resp.status_code == 404
    assert not calls


def test_an_idea_whose_draft_was_deleted_still_writes_again(
    client, auth, db, idea, calls
):
    """The locked read must not change the one case where a replay is wrong."""
    first = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    client.delete(f"/api/v1/content/{first.json()['id']}", headers=auth)

    again = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert again.status_code == 201, again.text
    assert again.json()["title"] == "Generated 2"
    assert len(calls) == 2


# ---- Both success codes are described ------------------------------------- #


def test_both_success_codes_say_which_one_they_are(client):
    """The endpoint has two 2xx answers, so neither may be left as FastAPI's
    generated "Successful Response" — a described 200 beside a bare 201 tells a
    reader which one is the replay and leaves them to guess the other."""
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/api/v1/content/ideas/{idea_id}/write"]["post"][
        "responses"
    ]

    successes = {c: b["description"] for c, b in responses.items() if c.startswith("2")}
    assert set(successes) == {"200", "201"}
    for code, description in successes.items():
        assert description != "Successful Response", f"{code} describes nothing"
    assert "already been written" in successes["200"]
    assert "retired" in successes["201"]


def test_both_success_codes_return_the_content_detail_model(client):
    """A replay that answered a different shape would make the 200 unusable
    without a second branch in every client."""
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/api/v1/content/ideas/{idea_id}/write"]["post"][
        "responses"
    ]

    for code in ("200", "201"):
        ref = responses[code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ContentDetail")
