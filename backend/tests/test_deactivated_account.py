"""Deactivating an account stops the work Herald does on its behalf.

``User.is_active`` was honoured everywhere a request carried a token — login,
:func:`app.deps.get_current_user`, the password-reset flow, the weekly digest —
and nowhere a background worker did. So deactivating an account stopped it
signing in and stopped nothing it had already set running: the autopilot kept
scanning its repos, its triggers kept firing (including the unauthenticated
inbound webhook, whose URL is the only credential), and both kept writing and
publishing with its stored platform credentials, on a schedule nobody could log
in to change.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.project import AutopilotMode, Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.trigger import Trigger, TriggerEventStatus, TriggerKind
from app.models.user import User
from app.security import hash_password
from app.services import content_pipeline, headlines, preview_links
from app.services import triggers as trigger_service
from app.services.signals import TriggerSignal
from app.tasks import autopilot_tasks, headline_tasks, publish_tasks


@pytest.fixture
def dormant(db) -> User:
    row = User(
        email="gone@example.com",
        full_name="Gone",
        hashed_password=hash_password("hunter2hunter2"),
        is_active=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def dormant_project(db, dormant) -> Project:
    row = Project(
        user_id=dormant.id,
        name="Abandoned",
        slug="abandoned",
        repo_url="https://github.com/r2st/Abandoned",
        autopilot_mode=AutopilotMode.AUTO,
        autopilot_platforms=["devto"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# The autopilot                                                                #
# --------------------------------------------------------------------------- #


def test_the_sweep_does_not_pick_up_a_dormant_account(
    db, dormant_project, project, monkeypatch
):
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    dispatched: list[int] = []
    monkeypatch.setattr(
        autopilot_tasks.scan_project, "delay", lambda pid: dispatched.append(pid)
    )

    autopilot_tasks.scan_all_projects()

    assert dormant_project.id not in dispatched
    assert project.id in dispatched


def test_scanning_one_project_by_id_checks_the_account_too(
    db, dormant_project, monkeypatch
):
    """The task is called by id from elsewhere, so the query guard is not enough."""
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    def _boom(*args, **kwargs):
        raise AssertionError("GitHub must not be reached for a dormant account")

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", _boom)

    result = autopilot_tasks.scan_project(dormant_project.id)

    assert result == {"project_id": dormant_project.id, "status": "skipped"}


# --------------------------------------------------------------------------- #
# Triggers                                                                     #
# --------------------------------------------------------------------------- #


def test_a_dormant_accounts_polled_trigger_is_not_due(db, dormant_project, project):
    for owner in (dormant_project, project):
        db.add(
            Trigger(
                project_id=owner.id,
                kind=TriggerKind.SCHEDULE,
                name=f"{owner.slug} weekly",
                config={"interval_hours": 1},
            )
        )
    db.commit()

    due = trigger_service.due_triggers(db)

    assert [t.project_id for t in due] == [project.id]


def test_an_inbound_webhook_for_a_dormant_account_writes_nothing(db, dormant_project):
    """The one unauthenticated endpoint, so this is the only place to stop it."""
    trigger = Trigger(
        project_id=dormant_project.id,
        kind=TriggerKind.WEBHOOK,
        name="Deploys",
        config={},
        token="t" * 43,
    )
    db.add(trigger)
    db.commit()

    event = trigger_service.fire(
        db,
        trigger,
        TriggerSignal(
            kind=TriggerKind.WEBHOOK,
            source="Webhook from Deploys",
            headline="We shipped",
            summary="A release went out",
        ),
    )

    assert event is not None
    assert event.status == TriggerEventStatus.SKIPPED
    assert db.query(Content).count() == 0


def test_the_inbound_endpoint_reports_nothing_about_why(client, db, dormant_project):
    """A caller holding the URL learns it was skipped, not that the owner is gone."""
    token = "u" * 43
    db.add(
        Trigger(
            project_id=dormant_project.id,
            kind=TriggerKind.WEBHOOK,
            name="Deploys",
            config={},
            token=token,
        )
    )
    db.commit()

    resp = client.post(f"/api/v1/triggers/inbound/{token}", json={"text": "shipped"})

    assert resp.status_code == 202
    assert resp.json()["status"] == "skipped"
    assert db.query(Content).count() == 0


# --------------------------------------------------------------------------- #
# Releasing approved content                                                   #
# --------------------------------------------------------------------------- #


def _approved(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Still going",
        slug="still-going",
        body_markdown="word " * 200,
        excerpt="x",
        meta_description="x",
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_approved_content_is_not_released_for_a_dormant_account(db, dormant_project):
    content = _approved(db, dormant_project)
    assert content_pipeline.release_approved(db, content) == []
    assert db.query(Publication).count() == 0


def test_the_release_sweep_skips_a_dormant_account(db, dormant_project, monkeypatch):
    monkeypatch.setattr(publish_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    _approved(db, dormant_project)

    assert publish_tasks.release_approved_content() == {"found": 0, "released": 0}


# --------------------------------------------------------------------------- #
# The headline sweep                                                           #
# --------------------------------------------------------------------------- #


def _headline_contest(db, project) -> Content:
    """A published piece with a past headline that clearly beat the live one.

    Enough evidence that ``auto_select_headlines`` would swap it back if it ever
    looked at this project — which is the whole point of the assertion below.
    """
    start = datetime.now(UTC) - timedelta(days=20)
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Original headline",
        slug=f"original-headline-{project.id}",
        body_markdown="word " * 200,
        status=ContentStatus.PUBLISHED,
        created_at=start,
    )
    db.add(content)
    db.commit()

    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(publication)
    db.commit()

    def snapshot(offset: int, views: int) -> None:
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=start + timedelta(days=offset),
                views=views,
            )
        )
        db.commit()

    snapshot(1, 2000)
    snapshot(9, 4000)
    headlines.apply_headline(content, "Challenger headline", now=start + timedelta(days=10))
    db.commit()
    snapshot(11, 4100)
    snapshot(19, 4200)

    project.auto_headline_winner = True
    db.commit()
    db.refresh(content)
    return content


def test_the_headline_sweep_skips_a_dormant_account(db, dormant_project, monkeypatch):
    """A daily sweep rewriting the titles of a dormant account's live posts."""
    content = _headline_contest(db, dormant_project)

    monkeypatch.setattr(headline_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    assert headline_tasks.auto_select_headlines() == {"considered": 0, "swapped": 0}
    db.refresh(content)
    assert content.title == "Challenger headline"


def test_the_headline_sweep_still_runs_for_a_live_account(db, project, monkeypatch):
    """The guard must not catch the ordinary case."""
    content = _headline_contest(db, project)

    monkeypatch.setattr(headline_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    assert headline_tasks.auto_select_headlines() == {"considered": 1, "swapped": 1}
    db.refresh(content)
    assert content.title == "Original headline"


# --------------------------------------------------------------------------- #
# Preview links                                                                #
# --------------------------------------------------------------------------- #


def _draft(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Half-written",
        slug=f"half-written-{project.id}",
        body_markdown="## Draft\n\n" + ("word " * 50),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_dormant_accounts_preview_link_stops_resolving(db, dormant_project):
    """The one door deactivation left open.

    Every other switch-off in this file is checked at the moment Herald acts.
    A preview link is checked when a *stranger* acts: no token, no account, an
    anonymous read of an unpublished draft, granted before the deactivation and
    outliving it by up to ``preview_link_max_ttl_hours``.
    """
    content = _draft(db, dormant_project)
    _, raw_token = preview_links.issue(db, content)

    assert preview_links.resolve(db, raw_token) is None


def test_the_public_preview_read_is_a_404_and_says_no_more(client, db, dormant_project):
    """Same 404 as an unknown or expired token — the holder learns nothing."""
    content = _draft(db, dormant_project)
    _, raw_token = preview_links.issue(db, content)

    resp = client.get(f"/api/v1/content/preview/{raw_token}")

    assert resp.status_code == 404
    assert "deactiv" not in resp.text.lower()


def test_a_link_issued_before_the_switch_off_dies_with_the_account(
    client, db, user, project
):
    """The realistic order of events: link out, reviewer holding it, then off."""
    content = _draft(db, project)
    _, raw_token = preview_links.issue(db, content)
    assert client.get(f"/api/v1/content/preview/{raw_token}").status_code == 200

    user.is_active = False
    db.commit()

    assert client.get(f"/api/v1/content/preview/{raw_token}").status_code == 404


def test_a_dead_preview_read_does_not_count_as_a_view(db, dormant_project):
    """A refused read must not look to the author like the link was opened."""
    content = _draft(db, dormant_project)
    row, raw_token = preview_links.issue(db, content)

    preview_links.resolve(db, raw_token)

    db.refresh(row)
    assert row.view_count == 0
    assert row.last_viewed_at is None


def test_a_live_accounts_preview_link_still_resolves(db, project):
    """The guard must not catch the ordinary case."""
    content = _draft(db, project)
    _, raw_token = preview_links.issue(db, content)

    assert preview_links.resolve(db, raw_token) is not None


# --------------------------------------------------------------------------- #
# What deactivation does *not* do                                              #
# --------------------------------------------------------------------------- #


def test_work_already_queued_still_goes_out(db, dormant_project):
    """Deactivation stops Herald starting new work, not finishing instructed work.

    A queued publication is an explicit instruction that predates the
    deactivation, and dropping it would strand the row with no error on it and
    nobody able to log in and see why.
    """
    from app.models.publication import Platform, PublicationStatus
    from app.services import publishing_service

    content = _approved(db, dormant_project)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PENDING,
        )
    )
    db.commit()

    due = publishing_service.due_publications(db)

    assert [p.content_id for p in due] == [content.id]


def test_a_dormant_account_cannot_reach_the_api(client, db, dormant):
    """The guard that already worked, pinned so it stays that way."""
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": dormant.email, "password": "hunter2hunter2"},
    )
    assert resp.status_code in (400, 401, 403)


def test_an_account_deactivated_mid_session_loses_its_token(client, db, user, auth):
    user.is_active = False
    db.commit()

    resp = client.get("/api/v1/projects", headers=auth)

    assert resp.status_code == 401


def test_the_scheduled_publish_of_a_live_account_is_unaffected(db, project):
    """The guards must not catch the ordinary case."""
    from app.models.publication import Platform, PublicationStatus
    from app.services import publishing_service

    content = _approved(db, project)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db.commit()

    assert len(publishing_service.due_publications(db)) == 1
