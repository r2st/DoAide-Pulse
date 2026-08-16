"""The three tag endpoints, and the one of them that writes.

``POST /tags/rename`` is the only write in Herald that acts on rows named by a
*string* rather than by id. There is no list of ids to check against, no undo —
tags are not a field the revision trail tracks — and the blast radius is every
piece in the account. That asymmetry is what the dry run is for, and most of
this file is about the dry run agreeing with the write.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project
from app.services import tags as tag_service


def _piece(db, project, n: int, tags: list[str]) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Piece {n}",
        slug=f"piece-{n}",
        body_markdown="Deploying Herald with Docker. Docker makes deployment "
        "repeatable, and deployment is the part people dread.",
        excerpt="Deploying Herald.",
        status=ContentStatus.DRAFT,
        tags=tags,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def tagged(db, project) -> list[Content]:
    return [
        _piece(db, project, 0, ["guides/docker", "news"]),
        _piece(db, project, 1, ["guides/deployment"]),
        _piece(db, project, 2, ["guides"]),
        _piece(db, project, 3, ["guides-advanced"]),
        _piece(db, project, 4, []),
    ]


# --------------------------------------------------------------------------- #
# The tree                                                                     #
# --------------------------------------------------------------------------- #


def test_the_tree_nests_and_counts(client, auth, tagged):
    resp = client.get("/api/v1/tags", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    by_tag = {node["tag"]: node for node in body["roots"]}
    assert set(by_tag) == {"guides", "news", "guides-advanced"}
    guides = by_tag["guides"]
    # One piece carries ``guides`` directly; three carry it or something under.
    assert guides["direct"] == 1
    assert guides["total"] == 3
    assert {child["tag"] for child in guides["children"]} == {
        "guides/docker",
        "guides/deployment",
    }
    assert guides["children"][0]["label"] in {"docker", "deployment"}
    assert body["scanned"] == 5
    assert body["truncated"] is False


def test_a_lookalike_is_its_own_root(client, auth, tagged):
    """``guides-advanced`` shares five letters with ``guides`` and is a
    different tag. If it ever nests under it, the separator check has gone."""
    roots = {node["tag"] for node in client.get("/api/v1/tags", headers=auth).json()["roots"]}
    assert "guides-advanced" in roots


def test_the_tree_can_be_narrowed_to_one_project(client, auth, db, user, tagged):
    other = Project(user_id=user.id, name="Other", slug="other")
    db.add(other)
    db.commit()
    db.refresh(other)
    _piece(db, other, 99, ["elsewhere"])

    everything = client.get("/api/v1/tags", headers=auth).json()
    narrowed = client.get(
        f"/api/v1/tags?project_id={other.id}", headers=auth
    ).json()
    assert "elsewhere" in {n["tag"] for n in everything["roots"]}
    assert {n["tag"] for n in narrowed["roots"]} == {"elsewhere"}


def test_a_truncated_scan_says_so(client, auth, tagged):
    """The counts become a floor, and a caller planning a rename against them
    deserves to know that before they do."""
    resp = client.get("/api/v1/tags?limit=2", headers=auth)
    body = resp.json()
    assert body["scanned"] == 2
    assert body["truncated"] is True


def test_the_scan_is_bounded_at_both_ends(client, auth):
    assert client.get("/api/v1/tags?limit=0", headers=auth).status_code == 422
    assert (
        client.get(
            f"/api/v1/tags?limit={tag_service.TREE_SCAN_LIMIT + 1}", headers=auth
        ).status_code
        == 422
    )


def test_another_account_sees_none_of_it(client, auth, db, tagged):
    """The tree is joined through ``Project.user_id``; this is the assertion
    that says so rather than trusting the query."""
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.commit()
    token = client.post(
        "/api/v1/auth/login",
        data={"username": "stranger@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]
    resp = client.get("/api/v1/tags", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["roots"] == []


# --------------------------------------------------------------------------- #
# The rename                                                                   #
# --------------------------------------------------------------------------- #


def test_a_dry_run_names_the_pieces_and_changes_none_of_them(
    client, auth, db, tagged
):
    resp = client.post(
        "/api/v1/tags/rename",
        json={"old": "guides", "new": "howto", "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["count"] == 3
    assert set(body["content_ids"]) == {tagged[0].id, tagged[1].id, tagged[2].id}
    db.expire_all()
    assert db.get(Content, tagged[0].id).tags == ["guides/docker", "news"]


def test_the_rename_moves_the_subtree(client, auth, db, tagged):
    resp = client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 3
    db.expire_all()
    assert db.get(Content, tagged[0].id).tags == ["howto/docker", "news"]
    assert db.get(Content, tagged[1].id).tags == ["howto/deployment"]
    assert db.get(Content, tagged[2].id).tags == ["howto"]


def test_the_rename_leaves_the_lookalike_alone(client, auth, db, tagged):
    client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    )
    db.expire_all()
    assert db.get(Content, tagged[3].id).tags == ["guides-advanced"]


def test_the_dry_run_and_the_rename_name_the_same_pieces(client, auth, tagged):
    """The property that makes the preview worth reading."""
    preview = client.post(
        "/api/v1/tags/rename",
        json={"old": "guides", "new": "howto", "dry_run": True},
        headers=auth,
    ).json()
    real = client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    ).json()
    assert preview["content_ids"] == real["content_ids"]
    assert preview["merged"] == real["merged"]
    assert preview["count"] == real["count"]


def test_a_merge_is_reported_rather_than_refused(client, auth, db, project):
    """Renaming onto a tag that already exists is a merge — legitimate, and the
    one outcome of a rename that loses information, so the pieces where two tags
    collapsed into one come back in ``merged``."""
    both = _piece(db, project, 7, ["guides", "howto"])
    only = _piece(db, project, 8, ["guides"])
    resp = client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    )
    body = resp.json()
    assert body["merged"] == [both.id]
    assert set(body["content_ids"]) == {both.id, only.id}
    db.expire_all()
    assert db.get(Content, both.id).tags == ["howto"]


def test_a_merge_between_two_children_is_still_a_merge(client, auth, db, project):
    """``howto`` itself appears nowhere, which is why the count comes from the
    list lengths rather than from looking for ``new`` in the original."""
    piece = _piece(db, project, 11, ["guides/x", "howto/x"])
    body = client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    ).json()
    assert body["merged"] == [piece.id]
    db.expire_all()
    assert db.get(Content, piece.id).tags == ["howto/x"]


def test_a_rename_finds_the_rows_that_were_written_before_any_of_this(
    client, auth, db, project
):
    """The stored lists predate the canonical form. A rename that only matched
    tidy spellings would leave the untidy rows behind — a half-done rename,
    which is the one outcome a vocabulary tool cannot afford."""
    messy = _piece(db, project, 12, ["Guides / Docker"])
    body = client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    ).json()
    assert body["content_ids"] == [messy.id]
    db.expire_all()
    assert db.get(Content, messy.id).tags == ["howto/docker"]


def test_the_rename_can_be_narrowed_to_one_project(client, auth, db, user, project):
    kept = _piece(db, project, 13, ["guides"])
    other = Project(user_id=user.id, name="Other", slug="other")
    db.add(other)
    db.commit()
    db.refresh(other)
    moved = _piece(db, other, 14, ["guides"])

    body = client.post(
        "/api/v1/tags/rename",
        json={"old": "guides", "new": "howto", "project_id": other.id},
        headers=auth,
    ).json()
    assert body["content_ids"] == [moved.id]
    db.expire_all()
    assert db.get(Content, kept.id).tags == ["guides"]
    assert db.get(Content, moved.id).tags == ["howto"]


def test_a_rename_never_reaches_another_account(client, auth, db, tagged):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.commit()
    db.refresh(stranger)
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)
    untouched = _piece(db, theirs, 21, ["guides"])

    client.post(
        "/api/v1/tags/rename", json={"old": "guides", "new": "howto"}, headers=auth
    )
    db.expire_all()
    assert db.get(Content, untouched.id).tags == ["guides"]


@pytest.mark.parametrize("field", ["old", "new"])
def test_a_tag_argument_that_survives_as_nothing_is_refused(client, auth, field):
    """A stored list drops ``"!!!"`` quietly, and costs the writer nothing they
    meant. A rename whose ``old`` normalises to the empty string is a rename of
    everything or of nothing depending on how the prefix match is written, and
    200 is the wrong answer either way."""
    payload = {"old": "guides", "new": "howto"}
    payload[field] = "!!!"
    resp = client.post("/api/v1/tags/rename", json=payload, headers=auth)
    assert resp.status_code == 422


def test_the_response_echoes_the_canonical_form(client, auth, tagged):
    """``Guides`` and ``guides`` are the same rename, and the response says
    which one was actually run."""
    body = client.post(
        "/api/v1/tags/rename",
        json={"old": "Guides", "new": "How To", "dry_run": True},
        headers=auth,
    ).json()
    assert body["old"] == "guides"
    assert body["new"] == "how-to"


# --------------------------------------------------------------------------- #
# The suggestions                                                              #
# --------------------------------------------------------------------------- #


def test_a_suggestion_prefers_a_tag_the_account_already_uses(
    client, auth, db, project, tagged
):
    """The whole point. A suggester that proposes a fifth spelling of
    ``deployment`` has made the vocabulary worse; one that proposes the tag
    eleven other pieces already carry has made it smaller."""
    fresh = _piece(db, project, 30, [])
    resp = client.post(f"/api/v1/tags/suggest/{fresh.id}", headers=auth)
    assert resp.status_code == 200
    suggestions = resp.json()
    assert suggestions[0]["tag"] in {"guides/docker", "guides/deployment"}
    assert "other" in suggestions[0]["reason"]
    # Ranked, and the account's own vocabulary outranks anything from the text.
    assert suggestions[0]["score"] > suggestions[-1]["score"]


def test_a_suggestion_is_never_one_the_piece_already_carries(
    client, auth, db, project, tagged
):
    """At any depth: a piece tagged ``guides/deployment`` is offered neither
    that nor ``guides``."""
    already = _piece(db, project, 31, ["guides/deployment"])
    proposed = {
        s["tag"] for s in client.post(
            f"/api/v1/tags/suggest/{already.id}", headers=auth
        ).json()
    }
    assert "guides/deployment" not in proposed
    assert "guides" not in proposed


def test_a_suggestion_falls_back_to_the_project_stack_and_the_body(
    client, auth, db, user
):
    """A first piece in a brand-new account, where every stronger source is
    empty — which is the account that most needs a starting point."""
    project = Project(
        user_id=user.id,
        name="Fresh",
        slug="fresh",
        tech_stack=["Docker"],
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    piece = _piece(db, project, 40, [])

    suggestions = client.post(
        f"/api/v1/tags/suggest/{piece.id}", headers=auth
    ).json()
    proposed = {s["tag"] for s in suggestions}
    assert "docker" in proposed
    # ``deployment`` appears twice and ``deploying`` once, so only the word the
    # piece keeps saying clears the frequency floor.
    assert "repeatable" not in proposed
    assert all(s["reason"] for s in suggestions)


def test_suggestions_are_bounded(client, auth, db, project):
    piece = _piece(db, project, 41, [])
    assert (
        len(client.post(f"/api/v1/tags/suggest/{piece.id}?limit=2", headers=auth).json())
        <= 2
    )
    assert (
        client.post(
            f"/api/v1/tags/suggest/{piece.id}?limit=999", headers=auth
        ).status_code
        == 422
    )


def test_suggesting_for_someone_elses_piece_is_a_404(client, auth, db, user):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.commit()
    db.refresh(stranger)
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)
    piece = _piece(db, theirs, 50, [])

    resp = client.post(f"/api/v1/tags/suggest/{piece.id}", headers=auth)
    assert resp.status_code == 404


def test_suggesting_writes_nothing(client, auth, db, project):
    """The caller applies what it wants through ``PATCH /content/{id}``."""
    piece = _piece(db, project, 51, ["news"])
    client.post(f"/api/v1/tags/suggest/{piece.id}", headers=auth)
    db.expire_all()
    assert db.get(Content, piece.id).tags == ["news"]
