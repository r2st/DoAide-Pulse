"""Branches in the routers, tasks and the seed that need a second row to reach.

Most of these are loop-continues: the arm that runs when the first thing the
loop looked at was not the thing being asked for. A single-row fixture takes the
match on the first pass and the skip never executes, which is why they survived
a suite with a test for each of these endpoints already in it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest

from app import seed as seed_module
from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import analytics_service, feeds, headlines
from app.tasks import headline_tasks
from tests.conftest import TestSession

SEED_EMAIL = "quiet-arms-seed@herald.example.com"


def _published(db, project, *, title: str, slug: str, platform: Platform, views: int):
    content = Content(
        project_id=project.id,
        title=title,
        slug=slug,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow() - timedelta(days=3),
        external_url=f"https://dev.to/x/{slug}",
    )
    db.add(publication)
    db.flush()
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=utcnow() - timedelta(days=2),
            views=views,
        )
    )
    db.commit()
    db.refresh(publication)
    return content, publication


# ---- The velocity curve endpoint scans past the ones it was not asked for --- #


def test_the_curve_endpoint_finds_a_publication_that_is_not_the_first_one(
    db, client, auth, project
):
    """The loop's skip arm.

    ``curves`` comes back newest-publication-first, so asking for the older of
    two is what makes the endpoint iterate rather than match immediately. With
    one publication in the fixture the match happens on the first pass every
    time and the scan is never exercised.
    """
    _, older = _published(
        db, project, title="Older", slug="older", platform=Platform.DEVTO, views=10
    )
    older.published_at = utcnow() - timedelta(days=30)
    db.commit()
    _published(
        db, project, title="Newer", slug="newer", platform=Platform.MASTODON, views=99
    )

    resp = client.get(f"/api/v1/analytics/velocity/{older.id}", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["publication_id"] == older.id
    assert body["title"] == "Older"
    assert body["points"]


def test_the_curve_endpoint_still_404s_after_scanning_every_curve(
    db, client, auth, project
):
    """The same loop, falling off the end — "not yours" and "not there" alike."""
    _published(db, project, title="Only", slug="only", platform=Platform.DEVTO, views=5)

    resp = client.get("/api/v1/analytics/velocity/99999", headers=auth)

    assert resp.status_code == 404


# ---- A publication that is neither published nor failed --------------------- #


def test_a_scheduled_publication_counts_as_neither_published_nor_failed(db, project):
    """``by_platform`` buckets on two statuses and ignores the rest.

    A row still waiting in the queue is not a delivery and not a rejection.
    Counting it as either would make the reliability column answer a question
    about the future.
    """
    content = Content(
        project_id=project.id,
        title="Queued",
        slug="queued",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.LINKEDIN,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=utcnow() + timedelta(days=1),
        )
    )
    db.commit()

    rows = {r["platform"]: r for r in analytics_service.by_platform(db, project.user_id)}

    assert rows["linkedin"]["published"] == 0
    assert rows["linkedin"]["failed"] == 0


# ---- PATCH sending an explicit null ----------------------------------------- #


def test_patching_a_list_field_to_null_is_refused_rather_than_stored(
    db, client, auth, project
):
    """The bug this guard exists for, on the column that showed it worst.

    ``autopilot_platforms`` is a JSON column, so writing Python ``None`` into it
    did not violate ``NOT NULL`` — SQLAlchemy stores it as the JSON value
    ``null``, the row was accepted, and every subsequent *read* of the project
    then failed response validation because the schema says ``list``. One PATCH
    left the project unreadable, including by the endpoint that would have let
    the user fix it.
    """
    resp = client.patch(
        f"/api/v1/projects/{project.id}",
        headers=auth,
        json={"autopilot_platforms": None},
    )

    assert resp.status_code == 422, resp.text
    assert "autopilot_platforms" in resp.json()["detail"]

    db.refresh(project)
    assert project.autopilot_platforms is not None
    assert client.get(f"/api/v1/projects/{project.id}", headers=auth).status_code == 200


@pytest.mark.parametrize("field", ["name", "description", "tone", "is_active", "keywords"])
def test_patching_any_non_nullable_project_field_to_null_is_refused(
    client, auth, project, field
):
    """Scalar columns took the same input to an ``IntegrityError`` and a 500.

    Bad input reported as a server fault, and one 500 per field. The set is
    derived from the mapper, so this parametrization is a sample of it rather
    than the definition.
    """
    resp = client.patch(
        f"/api/v1/projects/{project.id}", headers=auth, json={field: None}
    )

    assert resp.status_code == 422, resp.text
    assert field in resp.json()["detail"]


def test_patching_a_nullable_field_to_null_still_clears_it(db, client, auth, project):
    """The guard must not take away the one thing ``null`` is for.

    ``live_url`` is nullable, so clearing it is a legitimate request and the
    only way to say "this project has no live URL any more".
    """
    resp = client.patch(
        f"/api/v1/projects/{project.id}", headers=auth, json={"live_url": None}
    )

    assert resp.status_code == 200, resp.text
    db.refresh(project)
    assert project.live_url is None


def test_patching_a_non_nullable_content_field_to_null_is_refused(
    db, client, auth, project
):
    """The same guard on the other endpoint that applies a PATCH dict."""
    row = Content(
        project_id=project.id,
        title="Draft",
        slug="draft",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        body_markdown="Body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    resp = client.patch(
        f"/api/v1/content/{row.id}", headers=auth, json={"body_markdown": None}
    )

    assert resp.status_code == 422, resp.text
    assert "body_markdown" in resp.json()["detail"]
    db.refresh(row)
    assert row.body_markdown == "Body."


def test_a_patch_that_sets_nothing_to_null_is_untouched_by_the_guard(
    client, auth, project
):
    """The ordinary path still goes through."""
    resp = client.patch(
        f"/api/v1/projects/{project.id}",
        headers=auth,
        json={"description": "Still here.", "keywords": []},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Still here."
    assert resp.json()["keywords"] == []


# ---- The feed fetch that owns its client ------------------------------------ #


def test_a_fetch_with_no_client_supplied_closes_the_one_it_made(monkeypatch):
    """Every test hands ``fetch`` a transport; production hands it nothing.

    The ``finally`` that closes a client ``fetch`` created itself is therefore
    the one line of the connection handling that had never run — and leaking a
    client per feed is a file descriptor per feed on the scan sweep.
    """
    closed: list[bool] = []
    feed_xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><title>One</title><link>https://x.test/1</link></item>"
        b"</channel></rss>"
    )

    class _TrackedClient(httpx.Client):
        def close(self) -> None:
            closed.append(True)
            super().close()

    def _fake_client() -> httpx.Client:
        return _TrackedClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=feed_xml)
            )
        )

    monkeypatch.setattr(feeds, "_client", _fake_client)

    feed = feeds.fetch("https://example.com/feed.xml")

    assert [e.title for e in feed.entries] == ["One"]
    assert closed == [True]


def test_a_fetch_that_fails_still_closes_the_client_it_made(monkeypatch):
    """The ``finally`` matters most on the path that raises."""
    closed: list[bool] = []

    class _TrackedClient(httpx.Client):
        def close(self) -> None:
            closed.append(True)
            super().close()

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    monkeypatch.setattr(
        feeds,
        "_client",
        lambda: _TrackedClient(transport=httpx.MockTransport(_boom)),
    )

    with pytest.raises(feeds.FeedError):
        feeds.fetch("https://example.com/feed.xml")

    assert closed == [True]


# ---- The headline sweep's "nothing to do" arm ------------------------------- #


def test_the_headline_sweep_logs_and_moves_on_when_nothing_is_swapped(
    db, project, task_session, monkeypatch, caplog
):
    """The sweep considers a piece and declines it.

    Every existing test either has no candidates or has one that wins, so the
    branch that runs when a candidate is judged and left alone — by far the
    common case in production — had never executed.
    """
    monkeypatch.setattr(headline_tasks, "SessionLocal", task_session)
    project.auto_headline_winner = True
    db.add(
        Content(
            project_id=project.id,
            title="Current headline",
            slug="current-headline",
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.PUBLISHED,
            body_markdown="Body.",
            headline_history=[
                {
                    "title": "Older headline",
                    "started_at": (utcnow() - timedelta(days=20)).isoformat(),
                    "ended_at": (utcnow() - timedelta(days=10)).isoformat(),
                }
            ],
        )
    )
    db.commit()

    monkeypatch.setattr(
        headlines,
        "auto_select",
        lambda content, session: (
            headlines.Winner(
                title=content.title,
                reason="not enough evidence to move it",
                confident=False,
            ),
            False,
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="app.tasks.headline_tasks"):
        result = headline_tasks.auto_select_headlines()

    assert result["considered"] == 1
    assert result["swapped"] == 0
    assert "not enough evidence" in caplog.text


# ---- The seed, on a spec with nothing to suggest ---------------------------- #


def test_a_spec_with_no_ideas_seeds_the_project_and_says_nothing(db, user, caplog):
    """Two of the three shipped specs carry ideas; a spec without them is legal.

    The log line is conditional so a fresh install does not report seeding zero
    ideas, and that condition had only ever gone one way.
    """
    spec = seed_module.ProjectSpec(
        name="Bare",
        description="A project with nothing suggested yet.",
        repo_url="https://github.com/r2st/bare",
        live_url=None,
        tech_stack=["Python"],
        target_audience="Nobody in particular",
        keywords=["bare"],
        tone=Tone.TECHNICAL,
        autopilot_mode=AutopilotMode.OFF,
    )

    with caplog.at_level(logging.INFO, logger="app.seed"):
        project = seed_module._create_project(db, user.id, spec)
    db.commit()

    assert project.name == "Bare"
    assert db.query(ContentIdea).filter_by(project_id=project.id).count() == 0
    assert "content ideas" not in caplog.text


def test_a_seed_with_no_password_given_generates_one_and_prints_it_once(
    db, monkeypatch
):
    """The generated-password path.

    Every existing seed test passes a password so the run is reproducible, so
    the branch a real first install takes — generate one, log it once, never
    store it anywhere else — had never run.
    """
    monkeypatch.setattr(seed_module, "SessionLocal", TestSession)
    monkeypatch.delenv("SEED_PASSWORD", raising=False)
    monkeypatch.setenv("SEED_EMAIL", SEED_EMAIL)

    logged: list[str] = []
    monkeypatch.setattr(
        seed_module.logger, "info", lambda msg, *args: logged.append(msg % args if args else msg)
    )

    seed_module.seed()

    assert f"created user {SEED_EMAIL}" in logged
    printed = [line for line in logged if "shown once" in line]
    assert len(printed) == 1, "the generated password is announced exactly once"
    # A generated password, not the empty string a missing env var would give.
    assert printed[0].split("password: ")[1].split(" ")[0]
