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

    two_projects = len([s for s in sql_log if s.strip().upper().startswith("SELECT")])

    # Three more projects, each with content of its own. An N+1 would show up
    # as three extra SELECTs; the batched count and the eager trigger load mean
    # the number must not move at all. Asserting "unchanged" rather than a
    # magic ceiling means the test keeps its meaning when a query is legitimately
    # added or removed — as the X-Total-Count query was.
    for index in range(3):
        extra = Project(
            user_id=project.user_id, name=f"Extra {index}", slug=f"extra-{index}"
        )
        db.add(extra)
        db.flush()
        db.add(
            Content(
                project_id=extra.id,
                content_type=ContentType.TUTORIAL,
                title=f"Extra {index} guide",
                slug=f"extra-{index}-guide",
                status=ContentStatus.PUBLISHED,
            )
        )
    db.commit()

    sql_log.clear()
    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.status_code == 200
    assert len(resp.json()) == 5
    five_projects = len([s for s in sql_log if s.strip().upper().startswith("SELECT")])

    assert five_projects == two_projects, (
        f"query count grew with the number of projects: {two_projects} for two, "
        f"{five_projects} for five"
    )


def test_the_project_list_is_paginated_and_reports_the_total(
    client, auth, project, db
):
    """Nothing caps projects per account, so the listing has to cap itself.

    Unbounded, three things grow with the row count: the response, the eager
    load of every trigger on every project, and the ``IN`` clause
    ``_batch_counts`` builds from the ids — and Postgres refuses a statement
    with more than 65535 bind parameters outright rather than merely slowing
    down.
    """
    for index in range(6):
        db.add(
            Project(
                user_id=project.user_id, name=f"P{index:02d}", slug=f"p-{index:02d}"
            )
        )
    db.commit()

    resp = client.get("/api/v1/projects?limit=3", headers=auth)

    assert resp.status_code == 200
    assert len(resp.json()) == 3
    # Seven: the fixture's project plus the six above.
    assert resp.headers["X-Total-Count"] == "7"

    page_two = client.get("/api/v1/projects?limit=3&offset=3", headers=auth)
    assert page_two.status_code == 200
    assert len(page_two.json()) == 3
    assert page_two.headers["X-Total-Count"] == "7"

    first = [p["slug"] for p in resp.json()]
    second = [p["slug"] for p in page_two.json()]
    assert set(first).isdisjoint(second), "pages must not overlap"

    tail = client.get("/api/v1/projects?limit=3&offset=6", headers=auth)
    assert len(tail.json()) == 1


def test_the_project_list_refuses_a_limit_outside_its_bounds(client, auth):
    """``ge=1`` as well as ``le`` — see the note in ``list_content``.

    A negative limit reaches SQLAlchemy verbatim, and SQLite reads ``LIMIT -1``
    as "no limit", which would hand back the whole table the cap exists to
    withhold.
    """
    assert client.get("/api/v1/projects?limit=0", headers=auth).status_code == 422
    assert client.get("/api/v1/projects?limit=-1", headers=auth).status_code == 422
    assert client.get("/api/v1/projects?limit=501", headers=auth).status_code == 422
    assert client.get("/api/v1/projects?offset=-1", headers=auth).status_code == 422


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


def test_feed_404s_for_a_project_with_nothing_published(client, project, db):
    """An empty feed used to answer 200 and name the project in the channel.

    ``project_id`` is a small integer and this endpoint takes no token, so
    serving the channel block for a project with no published items published
    ``name``, ``description`` and ``live_url`` for every project on the
    instance to anyone willing to count. None of those three are published
    content.
    """
    db.add(
        Content(
            project_id=project.id, content_type=ContentType.HOW_TO,
            title="Unfinished thing", slug="unfinished-thing", status=ContentStatus.DRAFT,
        )
    )
    db.commit()

    resp = client.get(f"/api/v1/projects/{project.id}/feed.xml")
    assert resp.status_code == 404
    assert project.name not in resp.text
    assert project.description not in resp.text
    assert project.live_url not in resp.text


def test_feed_does_not_distinguish_empty_from_absent(client, project):
    """The two 404s must be one response.

    A different status, detail or body for "exists but published nothing"
    versus "no such project" answers the enumeration question anyway, just one
    step further along.
    """
    empty = client.get(f"/api/v1/projects/{project.id}/feed.xml")
    absent = client.get("/api/v1/projects/999999/feed.xml")

    assert empty.status_code == absent.status_code == 404
    assert empty.json() == absent.json()


def test_feed_returns_once_something_is_published(client, project, db):
    """The 404 above is about having no published items, not about the project.

    Guards the obvious over-correction: 404-ing the whole endpoint would also
    make this test pass if it only asserted on the empty case.
    """
    db.add(
        Content(
            project_id=project.id, content_type=ContentType.ANNOUNCEMENT,
            title="Herald 1.0", slug="herald-1-0", status=ContentStatus.PUBLISHED,
        )
    )
    db.commit()

    resp = client.get(f"/api/v1/projects/{project.id}/feed.xml")
    assert resp.status_code == 200
    assert "Herald 1.0" in resp.text


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
