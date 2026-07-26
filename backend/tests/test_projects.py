from __future__ import annotations

import pytest

from app.models.project import Project


def test_create_and_list_project(client, auth):
    resp = client.post(
        "/api/v1/projects",
        headers=auth,
        json={
            "name": "TalentPing",
            "description": "AI recruiter outreach.",
            "repo_url": "https://github.com/r2st/TalentPing",
            "tech_stack": ["FastAPI", "fastapi", "  React  ", ""],
            "keywords": ["job search"],
            "tone": "marketing",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "talentping"
    assert body["repo_full_name"] == "r2st/TalentPing"
    # Duplicates dropped case-insensitively, blanks removed, casing preserved.
    assert body["tech_stack"] == ["FastAPI", "React"]

    listed = client.get("/api/v1/projects", headers=auth).json()
    assert [p["name"] for p in listed] == ["TalentPing"]
    assert listed[0]["content_count"] == 0


def test_slug_collision_gets_suffixed(client, auth, project):
    resp = client.post("/api/v1/projects", headers=auth, json={"name": "Herald"})
    assert resp.status_code == 201
    assert resp.json()["slug"] == "herald-2"


def test_rename_reslugs_but_no_op_patch_does_not(client, auth, project):
    resp = client.patch(
        f"/api/v1/projects/{project.id}", headers=auth, json={"description": "Same name"}
    )
    assert resp.json()["slug"] == "herald"

    resp = client.patch(
        f"/api/v1/projects/{project.id}", headers=auth, json={"name": "Herald Two"}
    )
    assert resp.json()["slug"] == "herald-two"


def test_other_users_project_is_404(client, auth, db):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(email="x@example.com", hashed_password=hash_password("password123"))
    db.add(stranger)
    db.flush()
    theirs = Project(user_id=stranger.id, name="Secret", slug="secret")
    db.add(theirs)
    db.commit()

    assert client.get(f"/api/v1/projects/{theirs.id}", headers=auth).status_code == 404
    assert client.delete(f"/api/v1/projects/{theirs.id}", headers=auth).status_code == 404


def test_scan_without_repo_url_is_400(client, auth, db, user):
    project = Project(user_id=user.id, name="No Repo", slug="no-repo")
    db.add(project)
    db.commit()
    resp = client.post(f"/api/v1/projects/{project.id}/scan", headers=auth)
    assert resp.status_code == 400
    assert "no GitHub repo" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/r2st/Herald", "r2st/Herald"),
        ("https://github.com/r2st/Herald.git", "r2st/Herald"),
        ("git@github.com:r2st/Herald.git", "r2st/Herald"),
        ("https://github.com/r2st/Herald/", "r2st/Herald"),
        ("https://gitlab.com/r2st/Herald", None),
        ("not a url", None),
    ],
)
def test_repo_full_name_parsing(url, expected):
    assert Project(name="x", slug="x", repo_url=url).repo_full_name == expected
