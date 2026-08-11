"""Trigger HTTP paths that only run on a filter, a cap, or a bad key.

The happy paths live in ``test_trigger_api.py``. What is here is the branches
that a normal walk through the UI never reaches: filtering a list by project or
by event status, hitting the per-project ceiling, patching one field without
touching the others, and the two refusals that depend on machine state rather
than on what the caller sent.
"""
from __future__ import annotations

import pytest

from app.models.project import Project
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.user import User
from app.routers import triggers as trigger_router
from app.security import hash_password
from app.services import feeds
from app.services import triggers as trigger_service
from app.services.crypto import CredentialEncryptionError

API = "/api/v1/triggers"


def _make(client, auth, project, *, kind="webhook", name="t", **config):
    return client.post(
        API,
        json={
            "project_id": project.id,
            "kind": kind,
            "name": name,
            "config": config,
        },
        headers=auth,
    )


@pytest.fixture
def second_project(db, user) -> Project:
    row = Project(user_id=user.id, name="Other", slug="other")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_listing_filtered_by_project_excludes_the_other_projects_triggers(
    client, auth, project, second_project
):
    _make(client, auth, project, name="on herald")
    _make(client, auth, second_project, name="on other")

    resp = client.get(API, params={"project_id": project.id}, headers=auth)

    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()] == ["on herald"]


def test_the_trigger_list_is_paginated_and_reports_the_total(
    client, auth, project, second_project
):
    """The per-project cap is not a cap on the unnarrowed listing.

    MAX_TRIGGERS_PER_PROJECT holds each project to twenty, but nothing holds an
    account to a number of projects, so "all my triggers" was twenty times
    however many the caller had made.
    """
    for index in range(3):
        assert _make(client, auth, project, name=f"h{index}").status_code == 201
    for index in range(2):
        assert _make(client, auth, second_project, name=f"o{index}").status_code == 201

    resp = client.get(API, params={"limit": 2}, headers=auth)

    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert resp.headers["X-Total-Count"] == "5"

    page_two = client.get(API, params={"limit": 2, "offset": 2}, headers=auth)
    assert len(page_two.json()) == 2
    assert page_two.headers["X-Total-Count"] == "5"

    seen = [t["id"] for t in resp.json()] + [t["id"] for t in page_two.json()]
    assert len(set(seen)) == 4, "pages must not overlap"

    tail = client.get(API, params={"limit": 2, "offset": 4}, headers=auth)
    assert len(tail.json()) == 1


def test_the_total_counts_only_the_filtered_project(
    client, auth, project, second_project
):
    """X-Total-Count has to answer for the query that was asked, not the account.

    A total that ignored the project filter would make the UI render pagination
    for rows the caller cannot reach.
    """
    for index in range(3):
        _make(client, auth, project, name=f"h{index}")
    _make(client, auth, second_project, name="o0")

    resp = client.get(API, params={"project_id": project.id, "limit": 2}, headers=auth)

    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "3"
    assert len(resp.json()) == 2


def test_the_trigger_list_refuses_a_limit_outside_its_bounds(client, auth):
    assert client.get(API, params={"limit": 0}, headers=auth).status_code == 422
    assert client.get(API, params={"limit": -1}, headers=auth).status_code == 422
    assert client.get(API, params={"limit": 501}, headers=auth).status_code == 422
    assert client.get(API, params={"offset": -1}, headers=auth).status_code == 422


def test_filtering_by_a_project_that_is_not_yours_404s_rather_than_listing_nothing(
    client, auth, db
):
    """An empty list would confirm the id exists. The guard answers first."""
    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)

    resp = client.get(API, params={"project_id": theirs.id}, headers=auth)

    assert resp.status_code == 404


def test_the_twenty_first_trigger_on_a_project_is_refused(
    client, auth, project, db, monkeypatch
):
    monkeypatch.setattr(trigger_router, "MAX_TRIGGERS_PER_PROJECT", 2)
    assert _make(client, auth, project, name="one").status_code == 201
    assert _make(client, auth, project, name="two").status_code == 201

    resp = _make(client, auth, project, name="three")

    assert resp.status_code == 409
    assert "At most 2 triggers per project" in resp.json()["detail"]
    assert db.query(Trigger).count() == 2


def test_patching_only_the_name_leaves_the_config_alone(client, auth, project, db):
    created = _make(
        client, auth, project, kind="rss", feed_url="https://example.com/feed.xml"
    )
    trigger_id = created.json()["id"]

    resp = client.patch(
        f"{API}/{trigger_id}", json={"name": "  Renamed  "}, headers=auth
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    db.expire_all()
    assert db.get(Trigger, trigger_id).config["feed_url"] == "https://example.com/feed.xml"


def test_patching_a_config_the_kind_rejects_is_refused_and_nothing_is_written(
    client, auth, project, db
):
    created = _make(
        client, auth, project, kind="rss", feed_url="https://example.com/feed.xml"
    )
    trigger_id = created.json()["id"]

    resp = client.patch(
        f"{API}/{trigger_id}", json={"config": {"feed_url": ""}}, headers=auth
    )

    assert resp.status_code == 422
    db.expire_all()
    assert db.get(Trigger, trigger_id).config["feed_url"] == "https://example.com/feed.xml"


def test_checking_a_feed_that_cannot_be_reached_answers_with_the_reason(
    client, auth, project, monkeypatch
):
    """The button asks "does my feed URL work?" — a 500 does not answer it."""
    created = _make(
        client, auth, project, kind="rss", feed_url="https://example.com/feed.xml"
    )
    trigger_id = created.json()["id"]

    def unreachable(_url):
        raise feeds.FeedError("Could not reach that feed: ConnectError: refused")

    monkeypatch.setattr(trigger_service.feeds, "fetch", unreachable)

    resp = client.post(f"{API}/{trigger_id}/check", headers=auth)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "Could not reach that feed" in body["error"]


def test_events_can_be_filtered_to_one_status(client, auth, project, db):
    created = _make(client, auth, project)
    trigger_id = created.json()["id"]
    db.add_all(
        [
            TriggerEvent(
                trigger_id=trigger_id, status=TriggerEventStatus.SKIPPED, detail="dupe"
            ),
            TriggerEvent(
                trigger_id=trigger_id, status=TriggerEventStatus.FAILED, detail="boom"
            ),
        ]
    )
    db.commit()

    resp = client.get(
        f"{API}/{trigger_id}/events", params={"status": "failed"}, headers=auth
    )

    assert resp.status_code == 200
    assert [e["detail"] for e in resp.json()] == ["boom"]
    assert resp.headers["X-Total-Count"] == "1"


def test_an_inbound_body_past_the_cap_is_refused_before_it_is_decoded(
    client, auth, project, db
):
    _make(client, auth, project)
    token = db.query(Trigger).one().token

    resp = client.post(
        f"{API}/inbound/{token}",
        content=b"x" * (trigger_router.MAX_INBOUND_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert "128 KB" in resp.json()["detail"]
    assert db.query(TriggerEvent).count() == 0


def test_a_webhook_trigger_cannot_be_created_when_the_secret_cannot_be_encrypted(
    client, auth, project, monkeypatch
):
    """No key configured means the secret would be stored in the clear."""

    def explode(_secret):
        raise CredentialEncryptionError("TOKEN_ENCRYPTION_KEY is not set")

    monkeypatch.setattr(trigger_router.trigger_service, "store_secret", explode)

    resp = _make(client, auth, project)

    assert resp.status_code == 500
    assert "TOKEN_ENCRYPTION_KEY" in resp.json()["detail"]


def test_only_the_kinds_herald_goes_and_looks_at_are_polled():
    """``check`` is offered for these three and refused for the fourth."""
    assert TriggerKind.GITHUB.is_polled
    assert TriggerKind.RSS.is_polled
    assert TriggerKind.SCHEDULE.is_polled
    assert not TriggerKind.WEBHOOK.is_polled
