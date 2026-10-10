"""The project endpoints' unhappy halves.

Three things live here, none of which the happy-path tests reach: the slug retry
that runs when the uniqueness check loses a race, the translation of a GitHub
outage into an HTTP status the frontend can act on, and the ``/ideas`` endpoint's
refreshing half — the one that costs a model call.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.content import ContentIdea, ContentType
from app.models.project import Project
from app.services import content_generator, github_client

API = "/api/v1/projects"


def _payload(name: str = "Pulse", **extra) -> dict:
    return {"name": name, "description": "A thing that writes things.", **extra}


# --------------------------------------------------------------------------- #
# Slug collisions the pre-check cannot see                                     #
# --------------------------------------------------------------------------- #


def test_a_slug_race_lost_at_the_database_still_creates_the_project(
    client, auth, db, monkeypatch
):
    """``_unique_slug`` picks a free slug, then someone else takes it.

    The pre-check and the INSERT are two statements, so a concurrent create can
    land between them. The unique constraint catches the loser; the loser is
    supposed to retry with random hex rather than 500. Simulated by failing the
    first commit only — what a second writer would have caused.
    """
    real_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            db.rollback()
            raise IntegrityError("INSERT INTO projects", {}, Exception("unique"))
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)

    resp = client.post(API, json=_payload(), headers=auth)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The retry keeps the readable stem and appends disambiguating hex.
    assert body["slug"].startswith("pulse-")
    assert body["slug"] != "pulse"
    assert body["name"] == "Pulse"


def test_the_retry_builds_a_fresh_row_rather_than_reusing_the_rolled_back_one(
    client, auth, db, monkeypatch
):
    """A rolled-back instance is detached; re-adding it would persist stale state.

    The regression this guards: every field except the slug arriving empty
    because the second ``add`` reused an expunged object.
    """
    real_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            db.rollback()
            raise IntegrityError("INSERT INTO projects", {}, Exception("unique"))
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)

    resp = client.post(
        API,
        json=_payload(
            "Pulse",
            repo_url="https://github.com/r2st/DoAide-Pulse",
            tech_stack=["FastAPI"],
            keywords=["marketing"],
        ),
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["repo_url"] == "https://github.com/r2st/DoAide-Pulse"
    assert body["tech_stack"] == ["FastAPI"]
    assert body["keywords"] == ["marketing"]


def test_the_third_project_of_the_same_name_walks_past_the_second_suffix(
    client, auth
):
    """``pulse``, then ``pulse-2``, then ``pulse-3`` — the loop increments."""
    slugs = [
        client.post(API, json=_payload(), headers=auth).json()["slug"]
        for _ in range(3)
    ]

    assert slugs == ["pulse", "pulse-2", "pulse-3"]


def test_two_users_may_both_own_a_project_called_pulse(client, auth, db):
    """Slugs are unique per user, not globally."""
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    token = client.post(
        "/api/v1/auth/login",
        data={"username": other.email, "password": "hunter2hunter2"},
    ).json()["access_token"]

    mine = client.post(API, json=_payload(), headers=auth).json()
    theirs = client.post(
        API, json=_payload(), headers={"Authorization": f"Bearer {token}"}
    ).json()

    assert mine["slug"] == theirs["slug"] == "pulse"


def test_a_rename_onto_an_existing_name_gets_the_suffix_not_a_500(client, auth):
    first = client.post(API, json=_payload("Pulse"), headers=auth).json()
    second = client.post(API, json=_payload("Beacon"), headers=auth).json()

    resp = client.patch(
        f"{API}/{second['id']}", json={"name": "Pulse"}, headers=auth
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "pulse-2"
    assert first["slug"] == "pulse"


# --------------------------------------------------------------------------- #
# Repo scan: turning a GitHub failure into a status the UI can act on          #
# --------------------------------------------------------------------------- #


def test_a_github_rate_limit_becomes_a_429_carrying_the_reason(
    client, auth, project, monkeypatch
):
    """429, not 502: the caller should back off, not report an outage.

    The token is shared install-wide, so this is the one failure a user can fix
    by waiting — the message says so and the status says so.
    """
    def rate_limited(*args, **kwargs):
        raise github_client.GitHubRateLimited("GitHub rate limit exhausted; resets at 14:05 UTC")

    monkeypatch.setattr(github_client, "fetch_activity", rate_limited)

    resp = client.post(f"{API}/{project.id}/scan", headers=auth)

    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"].lower()


def test_any_other_github_failure_becomes_a_502(client, auth, project, monkeypatch):
    def broken(*args, **kwargs):
        raise github_client.GitHubError("GitHub returned 500")

    monkeypatch.setattr(github_client, "fetch_activity", broken)

    resp = client.post(f"{API}/{project.id}/scan", headers=auth)

    assert resp.status_code == 502
    assert "could not reach github" in resp.json()["detail"].lower()


def test_a_failed_scan_leaves_the_watermark_where_it_was(
    client, auth, project, db, monkeypatch
):
    """Moving it on failure would skip the commits the scan never saw."""
    project.last_seen_commit_sha = "abc123"
    db.commit()

    monkeypatch.setattr(
        github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(github_client.GitHubError("down")),
    )

    client.post(f"{API}/{project.id}/scan", headers=auth)

    db.refresh(project)
    assert project.last_seen_commit_sha == "abc123"
    assert project.last_scanned_at is None


def test_a_successful_scan_moves_the_watermark_and_summarises(
    client, auth, project, db, monkeypatch
):
    """The manual scan is the user saying "I've seen this"."""
    activity = github_client.RepoActivity(
        full_name="r2st/DoAide-Pulse",
        head_sha="deadbeef",
        new_commits=[
            github_client.Commit(
                sha=f"sha{i}",
                message=f"commit {i}",
                author="dev",
                committed_at=None,
                url="",
            )
            for i in range(25)
        ],
        latest_tag="v2.0",
        new_release=github_client.Release(
            tag="v2.0",
            name="v2.0",
            body="notes",
            published_at=None,
            url="",
            prerelease=False,
        ),
        stars=17,
        description="AI marketing automation",
        topics=["fastapi"],
    )
    monkeypatch.setattr(github_client, "fetch_activity", lambda *a, **k: activity)

    resp = client.post(f"{API}/{project.id}/scan", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["new_commit_count"] == 25
    assert body["new_release_tag"] == "v2.0"
    assert body["stars"] == 17
    # The summary list is capped; the count is not.
    assert len(body["commits"]) == 20

    db.refresh(project)
    assert project.last_seen_commit_sha == "deadbeef"
    assert project.last_seen_release_tag == "v2.0"
    assert project.last_scanned_at is not None


# --------------------------------------------------------------------------- #
# Ideas: the free read and the model call                                      #
# --------------------------------------------------------------------------- #


def test_ideas_without_refresh_reads_what_is_banked_and_calls_no_model(
    client, auth, project, db, monkeypatch
):
    db.add(
        ContentIdea(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            headline="Banked idea",
            rationale="Already on file",
            source={"kind": "autopilot"},
        )
    )
    db.commit()

    def fail(*args, **kwargs):  # pragma: no cover - asserted by not being called
        raise AssertionError("the cheap read must not reach the model")

    monkeypatch.setattr(content_generator, "suggest_ideas", fail)

    resp = client.get(f"{API}/{project.id}/ideas", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [i["headline"] for i in resp.json()] == ["Banked idea"]


def test_refresh_banks_what_the_model_returns_and_marks_it_manual(
    client, auth, project, db, monkeypatch
):
    monkeypatch.setattr(
        content_generator,
        "suggest_ideas",
        lambda project: [
            content_generator.Idea(
                content_type=ContentType.TUTORIAL,
                headline="Fresh idea",
                rationale="Because the model said so",
            )
        ],
    )

    resp = client.get(f"{API}/{project.id}/ideas?refresh=true", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [i["headline"] for i in resp.json()] == ["Fresh idea"]

    stored = db.query(ContentIdea).one()
    # Provenance matters: these are distinguishable from the autopilot's own.
    assert stored.source == {"kind": "manual_refresh"}


def test_used_ideas_are_not_offered_again(client, auth, project, db, monkeypatch):
    from app.models.content import Content, ContentStatus

    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        title="Written",
        slug="written",
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    db.add_all(
        [
            ContentIdea(
                project_id=project.id,
                content_type=ContentType.TUTORIAL,
                headline="Spent",
                rationale="r",
                used_content_id=content.id,
            ),
            ContentIdea(
                project_id=project.id,
                content_type=ContentType.TUTORIAL,
                headline="Unspent",
                rationale="r",
            ),
        ]
    )
    db.commit()

    resp = client.get(f"{API}/{project.id}/ideas", headers=auth)

    assert [i["headline"] for i in resp.json()] == ["Unspent"]


def test_the_idea_list_is_capped_at_a_dozen(client, auth, project, db):
    db.add_all(
        ContentIdea(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            headline=f"Idea {i}",
            rationale="r",
        )
        for i in range(20)
    )
    db.commit()

    resp = client.get(f"{API}/{project.id}/ideas", headers=auth)

    assert len(resp.json()) == 12


def test_ideas_for_someone_else_s_project_are_a_404(client, auth, db, user):
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="third@example.com",
        full_name="Third",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    theirs = Project(user_id=other.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()

    resp = client.get(f"{API}/{theirs.id}/ideas", headers=auth)

    assert resp.status_code == 404


@pytest.mark.parametrize("spelling", ["1", "t", "true", "yes", "on", "TRUE", "  on  "])
def test_every_spelling_fastapi_reads_as_true_also_counts_against_the_limit(spelling):
    """``_not_refreshing`` must agree with pydantic about what "true" means.

    A limit that exempts ``?refresh=on`` while the endpoint honours it is an
    unlimited endpoint, so the two readings are pinned together here rather than
    left to drift.
    """
    from starlette.datastructures import QueryParams

    from app.routers.projects import _not_refreshing

    request = type("R", (), {"query_params": QueryParams({"refresh": spelling})})()

    assert _not_refreshing(request) is False


@pytest.mark.parametrize("spelling", ["", "0", "false", "no", "off", "maybe"])
def test_the_cheap_read_stays_exempt(spelling):
    from starlette.datastructures import QueryParams

    from app.routers.projects import _not_refreshing

    request = type("R", (), {"query_params": QueryParams({"refresh": spelling})})()

    assert _not_refreshing(request) is True
