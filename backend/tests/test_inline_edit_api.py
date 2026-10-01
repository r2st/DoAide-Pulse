"""`POST /content/{id}/edit` — the endpoint around the inline editor.

The service is tested in ``test_inline_edit``. What is left here is what the
endpoint adds on top, and all of it is about the gap between the editor's copy
of a draft and the stored one:

The selection must appear verbatim in the saved body. That is the check doing
the real work — it stops a stale editor asking for an edit to a paragraph that
no longer exists and splicing the answer over whatever is there now, and it
means the endpoint cannot be used to run arbitrary text through the provider
chain on the account's quota.

Nothing is persisted, deliberately, and that is asserted rather than assumed:
a server-side apply would need a revision model to be safe, and an endpoint
that quietly rewrote a published post would be the worst version of this
feature.
"""
from __future__ import annotations

import json

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.services import inline_edit, llm_router

BODY = """## Why the queue exists

Publishing runs in a worker rather than in the request, because a platform that
takes thirty seconds to answer should not hold a browser open for that long.

Each publication is a row, one per platform, so "published to Dev.to, rejected
by LinkedIn" is a state the model can hold rather than an error.
"""

PASSAGE = (
    'Each publication is a row, one per platform, so "published to Dev.to, '
    'rejected\nby LinkedIn" is a state the model can hold rather than an error.'
)

URL = "/api/v1/content/{}/edit"


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="How Pulse publishes",
        slug="how-herald-publishes",
        body_markdown=BODY,
        excerpt="One row per platform.",
        meta_description="One row per platform.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def stub_llm(monkeypatch):
    state = {"payload": {"replacement": "One row per platform. That is the trick."}}

    def fake_complete(messages, *, model=None, fallback_models=(), **kwargs):
        payload = state["payload"]
        return llm_router.Completion(
            text=payload if isinstance(payload, str) else json.dumps(payload),
            provider="stub",
            model="stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    return state


def _post(client, auth, content, **overrides):
    body = {"selection": PASSAGE, "operation": "rewrite"}
    body.update(overrides)
    return client.post(URL.format(content.id), headers=auth, json=body)


# --------------------------------------------------------------------------- #
# The happy path                                                               #
# --------------------------------------------------------------------------- #


def test_an_edit_comes_back_with_the_replacement(client, auth, content, stub_llm):
    resp = _post(client, auth, content)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["replacement"] == "One row per platform. That is the trick."
    assert payload["operation"] == "rewrite"
    assert payload["provider"] == "stub"
    assert payload["model"] == "stub-model"


def test_nothing_is_persisted(client, auth, content, stub_llm, db):
    """The author decides whether the model improved anything.

    Persisting here would also mean the editor's unsaved changes were silently
    discarded by an action that reads as "improve this paragraph".
    """
    _post(client, auth, content)

    db.refresh(content)
    assert content.body_markdown == BODY


@pytest.mark.parametrize("operation", [op.value for op in inline_edit.EditOperation])
def test_every_operation_is_accepted_by_the_endpoint(
    client, auth, content, stub_llm, operation
):
    """The schema takes the enum straight from the service.

    Worth the parametrize: an operation the service knows and the API refuses
    is invisible until somebody clicks it.
    """
    if operation == "shorten":
        stub_llm["payload"] = {"replacement": "One row per platform."}
    resp = _post(client, auth, content, operation=operation)
    assert resp.status_code == 200, resp.text


def test_retone_accepts_an_explicit_tone(client, auth, content, stub_llm):
    resp = _post(client, auth, content, operation="retone", tone="casual")
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# The selection has to be real                                                 #
# --------------------------------------------------------------------------- #


def test_a_passage_that_is_not_in_the_draft_is_refused(client, auth, content, stub_llm):
    """The editor is out of date, and splicing the answer back would corrupt it."""
    resp = _post(client, auth, content, selection="A paragraph nobody ever wrote.")

    assert resp.status_code == 422
    assert "not in the saved draft" in resp.json()["detail"]


def test_the_refusal_happens_before_the_model_is_called(
    client, auth, content, monkeypatch
):
    """Otherwise the endpoint is a way to spend the account's daily quota on
    arbitrary text that has nothing to do with any draft."""
    calls = []

    def fake_complete(*args, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        raise AssertionError("the provider chain should not have been reached")

    monkeypatch.setattr(llm_router, "complete", fake_complete)

    resp = _post(client, auth, content, selection="Write me a poem about ducks.")
    assert resp.status_code == 422
    assert calls == []


def test_a_selection_of_whitespace_is_refused(client, auth, content, stub_llm):
    resp = _post(client, auth, content, selection=" " * 40)
    assert resp.status_code == 422


def test_a_selection_too_short_to_edit_is_refused(client, auth, content, stub_llm):
    resp = _post(client, auth, content, selection="a row")
    assert resp.status_code == 422


def test_a_selection_larger_than_the_cap_is_refused(client, auth, content, stub_llm):
    """Past the cap the honest answer is "regenerate the piece"."""
    resp = _post(
        client, auth, content, selection="x" * (inline_edit.MAX_SELECTION_CHARS + 1)
    )
    assert resp.status_code == 422


def test_an_unknown_operation_is_refused(client, auth, content, stub_llm):
    resp = _post(client, auth, content, operation="make_it_pop")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# When the model cannot help                                                   #
# --------------------------------------------------------------------------- #


def test_a_dead_provider_chain_is_a_503_not_a_500(client, auth, content, monkeypatch):
    """Nothing here is broken — a dependency is unavailable, and retrying is
    the right move. A 500 tells the user to file a bug instead."""

    def dead(*args, **kwargs):
        raise llm_router.AllProvidersFailed("everything is down")

    monkeypatch.setattr(llm_router, "complete", dead)

    resp = _post(client, auth, content)
    assert resp.status_code == 503
    assert "by hand" in resp.json()["detail"]


def test_an_unusable_reply_is_a_503_with_a_reason(client, auth, content, stub_llm):
    stub_llm["payload"] = {"replacement": ""}
    resp = _post(client, auth, content)

    assert resp.status_code == 503
    assert "returned nothing" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Ownership                                                                    #
# --------------------------------------------------------------------------- #


def test_someone_elses_draft_is_a_404(client, auth, db, stub_llm):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    other_project = Project(
        user_id=other.id, name="Other", slug="other", tone=Tone.TECHNICAL
    )
    db.add(other_project)
    db.commit()
    theirs = Content(
        project_id=other_project.id,
        content_type=ContentType.HOW_TO,
        title="Not yours",
        slug="not-yours",
        body_markdown=BODY,
    )
    db.add(theirs)
    db.commit()

    resp = client.post(
        URL.format(theirs.id),
        headers=auth,
        json={"selection": PASSAGE, "operation": "rewrite"},
    )
    assert resp.status_code == 404


def test_the_endpoint_needs_a_token(client, content):
    resp = client.post(
        URL.format(content.id), json={"selection": PASSAGE, "operation": "rewrite"}
    )
    assert resp.status_code == 401


def test_a_published_piece_can_still_be_edited(client, auth, content, stub_llm, db):
    """Fixing a typo in a live post is the case, not an edge case.

    Safe because nothing is persisted: the endpoint suggests, and republishing
    is a separate, deliberate act.
    """
    content.status = ContentStatus.PUBLISHED
    db.commit()

    assert _post(client, auth, content).status_code == 200
