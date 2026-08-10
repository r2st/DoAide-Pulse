"""``POST /content/ideas/{id}/write`` is idempotent per idea.

Writing an idea calls a model, which takes long enough that the button looks
broken. The second click used to sail straight past ``used_content_id`` — which
the endpoint *sets* but never *read* — spend a second generation, and leave the
user two near-identical drafts to reconcile by hand. An idea is a one-shot
prompt, not a "generate again" button; the second call has to return the draft
the first one produced.

The generator is stubbed throughout: what is being asserted is how many times
it is called, so a real one would only make the test slow and non-deterministic.
"""
from __future__ import annotations

import pytest

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


def test_writing_an_idea_produces_a_draft(client, auth, db, idea, calls):
    resp = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "Generated 1"
    assert len(calls) == 1
    db.refresh(idea)
    assert idea.used_content_id == resp.json()["id"]


def test_a_second_write_returns_the_first_draft_rather_than_making_another(
    client, auth, db, idea, calls
):
    first = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    assert first.status_code == 201, first.text

    second = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert second.status_code == 200, second.text
    # Same draft, not a second one with the same words.
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["title"] == "Generated 1"


def test_a_second_write_does_not_spend_another_model_call(
    client, auth, db, idea, calls
):
    """The expensive half. A double-click on a slow button cost two
    generations against the account's quota for one piece of content.
    """
    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert len(calls) == 1


def test_a_second_write_does_not_leave_a_duplicate_draft(
    client, auth, db, idea, calls
):
    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert db.query(Content).filter(Content.project_id == idea.project_id).count() == 1


def test_an_idea_whose_draft_was_deleted_can_be_written_again(
    client, auth, db, idea, calls
):
    """``used_content_id`` is not a foreign key on purpose — an idea outlives
    the draft a user deletes. With nothing left to return, the guard has to get
    out of the way rather than point at a row that is gone.
    """
    first = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)
    assert first.status_code == 201, first.text

    resp = client.delete(f"/api/v1/content/{first.json()['id']}", headers=auth)
    assert resp.status_code in (200, 204), resp.text

    again = client.post(f"/api/v1/content/ideas/{idea.id}/write", headers=auth)

    assert again.status_code == 201, again.text
    # A second generation, not the deleted one replayed. Asserted on the title
    # rather than the id: SQLite hands the freed rowid straight back, so the
    # new draft can legitimately land on the number the old one had.
    assert again.json()["title"] == "Generated 2"
    assert len(calls) == 2
    db.refresh(idea)
    assert idea.used_content_id == again.json()["id"]


def test_writing_an_idea_that_does_not_exist_is_a_404(client, auth, calls):
    resp = client.post("/api/v1/content/ideas/999999/write", headers=auth)

    assert resp.status_code == 404
    assert not calls


def test_writing_another_users_idea_does_not_reach_the_model(
    client, auth, db, calls, user
):
    """The ownership check is ``owned_project``, and it has to come *before*
    the generation — otherwise a stranger's idea id spends this account's quota
    before being told no.
    """
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
