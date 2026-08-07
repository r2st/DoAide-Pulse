from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
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


def test_list_projects_uses_batched_counts(client, auth, project, db, sql_log):
    """list_projects should fetch content counts in one query, not N+1."""
    # Create a second project so there's something to iterate over.
    p2 = Project(user_id=project.user_id, name="GoSumo", slug="gosumo")
    db.add(p2)
    db.flush()
    # Seed some content so counts are non-trivial.
    for p in (project, p2):
        db.add(Content(
            project_id=p.id, content_type=ContentType.ANNOUNCEMENT,
            title=f"{p.name} v1", slug=f"{p.slug}-v1",
            status=ContentStatus.PUBLISHED,
        ))
        db.add(Content(
            project_id=p.id, content_type=ContentType.TUTORIAL,
            title=f"{p.name} guide", slug=f"{p.slug}-guide",
            status=ContentStatus.DRAFT,
        ))
    db.commit()

    sql_log.clear()
    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    # project fixture ("Herald") + p2 ("GoSumo")
    assert len(body) == 2

    # Verify counts are correct.
    by_slug = {p["slug"]: p for p in body}
    assert by_slug["herald"]["content_count"] == 2
    assert by_slug["herald"]["published_count"] == 1
    assert by_slug["gosumo"]["content_count"] == 2
    assert by_slug["gosumo"]["published_count"] == 1

    # The N+1 fix: auth (user + selectin connections), projects, batched counts
    # = 4 SELECTs.  Without the fix it would be 2 + N (one count per project).
    selects = [s for s in sql_log if s.strip().upper().startswith("SELECT")]
    assert len(selects) <= 5, (
        f"Expected ≤5 SELECTs (auth + projects + batched counts), got {len(selects)}"
    )


# --------------------------------------------------------------------------- #
# RSS feed                                                                     #
# --------------------------------------------------------------------------- #


def test_feed_is_public_and_lists_only_published_content(client, project, db):
    """No Authorization header at all — an RSS reader has none to send."""
    published = Content(
        project_id=project.id, content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0", slug="herald-1-0", status=ContentStatus.PUBLISHED,
        excerpt="It's out.",
    )
    draft = Content(
        project_id=project.id, content_type=ContentType.HOW_TO,
        title="Unfinished thing", slug="unfinished-thing", status=ContentStatus.DRAFT,
    )
    db.add_all([published, draft])
    db.commit()

    resp = client.get(f"/api/v1/projects/{project.id}/feed.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert "Herald 1.0" in resp.text
    assert "Unfinished thing" not in resp.text


def test_feed_404s_for_a_nonexistent_project(client):
    resp = client.get("/api/v1/projects/999999/feed.xml")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Platform casing                                                              #
# --------------------------------------------------------------------------- #


def test_list_survives_uppercase_autopilot_platforms(client, auth, project, db):
    """A regression guard for a 500 on ``GET /projects``.

    ``autopilot_platforms`` is plain JSON, so whatever was written is what
    comes back — and rows exist holding enum *names* (``"DEVTO"``) rather than
    values. ``ProjectOut`` used to reject those and take the whole list
    endpoint down with it.
    """
    project.autopilot_platforms = ["DEVTO", "BLUESKY"]
    db.commit()

    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200, resp.text
    # Read tolerantly, write one spelling: the frontend only knows lower case.
    assert resp.json()[0]["autopilot_platforms"] == ["devto", "bluesky"]


def test_publish_accepts_either_platform_spelling(client, auth, project, db):
    """The same tolerance on the way in, so a caller may send either casing."""
    from app.models.publication import Platform

    assert Platform("DEVTO") is Platform.DEVTO
    assert Platform("devto") is Platform.DEVTO
    with pytest.raises(ValueError):
        Platform("not-a-platform")


# --------------------------------------------------------------------------- #
# Standing settings only name platforms Herald can actually publish to         #
# --------------------------------------------------------------------------- #
#
# ``POST /content/{id}/publish`` has always checked the adapter registry and
# answered 400 for an unfinished one. The two *standing* settings took any enum
# member, and the autopilot is where that bites: an auto-published piece routed
# to an unfinished adapter is written, approved, queued, and failed terminally
# by NotImplementedAdapter — which drives the content row to ``failed`` rather
# than into the review queue. Nobody reviews it and nobody publishes it, once
# per scan.


def _payload(**overrides) -> dict:
    body = {"name": "TalentPing", "description": "AI recruiter outreach."}
    body.update(overrides)
    return body


def test_autopilot_cannot_be_pointed_at_an_unfinished_adapter(client, auth):
    resp = client.post(
        "/api/v1/projects",
        headers=auth,
        json=_payload(autopilot_platforms=["devto", "twitter"]),
    )
    assert resp.status_code == 422, resp.text
    # The message names what does work, like the publish endpoint's does.
    assert "devto" in resp.text
    assert "twitter" in resp.text


def test_the_canonical_platform_cannot_be_an_unfinished_adapter(client, auth):
    resp = client.post(
        "/api/v1/projects",
        headers=auth,
        json=_payload(canonical_platform="linkedin"),
    )
    assert resp.status_code == 422, resp.text


def test_a_patch_is_guarded_the_same_way(client, auth, project):
    resp = client.patch(
        f"/api/v1/projects/{project.id}",
        headers=auth,
        json={"autopilot_platforms": ["linkedin"]},
    )
    assert resp.status_code == 422, resp.text


def test_finished_adapters_still_go_through(client, auth):
    resp = client.post(
        "/api/v1/projects",
        headers=auth,
        json=_payload(
            autopilot_platforms=["devto", "bluesky"], canonical_platform="devto"
        ),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["autopilot_platforms"] == ["devto", "bluesky"]


def test_a_repeated_platform_is_stored_once(client, auth):
    """Two rows for one platform is impossible anyway — the config should say so."""
    resp = client.post(
        "/api/v1/projects",
        headers=auth,
        json=_payload(autopilot_platforms=["devto", "devto", "bluesky"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["autopilot_platforms"] == ["devto", "bluesky"]


def test_clearing_the_canonical_platform_is_still_allowed(client, auth, project):
    resp = client.patch(
        f"/api/v1/projects/{project.id}",
        headers=auth,
        json={"canonical_platform": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["canonical_platform"] is None


def test_a_project_holding_a_stale_platform_is_still_readable(client, auth, project, db):
    """The check guards the way in only.

    A row written before it existed must stay readable, or the endpoint the user
    would fix it through is the endpoint that breaks.
    """
    project.autopilot_platforms = ["twitter"]
    db.commit()

    resp = client.get(f"/api/v1/projects/{project.id}", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["autopilot_platforms"] == ["twitter"]
