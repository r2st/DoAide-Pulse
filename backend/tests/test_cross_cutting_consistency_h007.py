"""H007 cross-cutting consistency: project_id guards, name stripping, rotation logging.

Four dimensions where one router did it right and another did not:

1. **project_id ownership on filtered listings.** Templates, API keys and triggers
   all call ``owned_project()`` when a ``project_id`` query/body parameter narrows
   a listing — an unknown or unowned id is a 404, not an empty page. Calendar and
   tags did not, so ``GET /calendar?project_id=99`` for a stranger's project 99
   returned an empty calendar rather than 404.

2. **Name stripping.** Templates, triggers and API keys all strip whitespace from
   names on create. Projects did not, so ``" My Project "`` was stored with its
   whitespace.

3. **Rotation logging.** Every delete endpoint logs, but none of the three
   credential rotation operations did — a security-sensitive lifecycle event
   invisible to the journal.
"""
from __future__ import annotations

import logging

import pytest

from app.models.api_key import ApiKeyScope
from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone
from app.models.trigger import Trigger, TriggerKind
from app.models.user import User
from app.models.webhook import Webhook, WebhookEvent
from app.security import hash_password
from app.services import api_keys as api_key_service
from app.services import webhooks as webhook_service

V1 = "/api/v1"


@pytest.fixture
def stranger(db) -> User:
    row = User(
        email="stranger-h007@example.com",
        full_name="H007 Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def stranger_project(db, stranger) -> Project:
    row = Project(
        user_id=stranger.id,
        name="Stranger Project",
        slug="stranger-project",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _lines(caplog, logger: str, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == logger and r.levelno == level
    ]


# --------------------------------------------------------------------------- #
# 1. project_id ownership on calendar                                         #
# --------------------------------------------------------------------------- #


def test_calendar_404s_on_someone_elses_project_id(
    client, auth, stranger_project
):
    resp = client.get(
        f"{V1}/calendar",
        headers=auth,
        params={"project_id": stranger_project.id},
    )
    assert resp.status_code == 404


def test_calendar_404s_on_nonexistent_project_id(client, auth):
    resp = client.get(
        f"{V1}/calendar",
        headers=auth,
        params={"project_id": 999999},
    )
    assert resp.status_code == 404


def test_calendar_still_works_with_own_project_id(client, auth, project):
    resp = client.get(
        f"{V1}/calendar",
        headers=auth,
        params={"project_id": project.id},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 2. project_id ownership on tag tree                                         #
# --------------------------------------------------------------------------- #


def test_tag_tree_404s_on_someone_elses_project_id(
    client, auth, stranger_project
):
    resp = client.get(
        f"{V1}/tags",
        headers=auth,
        params={"project_id": stranger_project.id},
    )
    assert resp.status_code == 404


def test_tag_tree_404s_on_nonexistent_project_id(client, auth):
    resp = client.get(
        f"{V1}/tags",
        headers=auth,
        params={"project_id": 999999},
    )
    assert resp.status_code == 404


def test_tag_tree_still_works_with_own_project_id(client, auth, project):
    resp = client.get(
        f"{V1}/tags",
        headers=auth,
        params={"project_id": project.id},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 3. project_id ownership on tag rename                                       #
# --------------------------------------------------------------------------- #


def test_tag_rename_404s_on_someone_elses_project_id(
    client, db, auth, user, project, stranger_project
):
    piece = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Tagged",
        slug="tagged",
        status=ContentStatus.DRAFT,
        tags=["old-tag"],
    )
    db.add(piece)
    db.commit()

    resp = client.post(
        f"{V1}/tags/rename",
        headers=auth,
        json={
            "old": "old-tag",
            "new": "new-tag",
            "project_id": stranger_project.id,
        },
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 4. Project name stripping                                                   #
# --------------------------------------------------------------------------- #


def test_project_name_is_stripped_on_create(client, auth):
    resp = client.post(
        f"{V1}/projects",
        headers=auth,
        json={"name": "  Padded Name  "},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Padded Name"


def test_project_name_is_stripped_on_update(client, auth, project):
    resp = client.patch(
        f"{V1}/projects/{project.id}",
        headers=auth,
        json={"name": "  Updated  "},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


def test_whitespace_only_project_name_is_rejected(client, auth):
    resp = client.post(
        f"{V1}/projects",
        headers=auth,
        json={"name": "   "},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 5. Rotation logging — webhooks                                              #
# --------------------------------------------------------------------------- #


def test_webhook_secret_rotation_is_logged(client, db, auth, user, caplog):
    hook = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhook_service.store_secret("original"),
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)

    with caplog.at_level(logging.INFO, logger="app.routers.webhooks"):
        resp = client.post(
            f"{V1}/webhooks/{hook.id}/rotate-secret", headers=auth
        )
    assert resp.status_code == 200

    lines = _lines(caplog, "app.routers.webhooks", logging.INFO)
    assert any(
        f"webhook {hook.id}" in line and "rotated" in line
        for line in lines
    )


# --------------------------------------------------------------------------- #
# 6. Rotation logging — triggers                                              #
# --------------------------------------------------------------------------- #


def test_trigger_secret_rotation_is_logged(client, db, auth, user, project, caplog):
    trigger = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="rotate-me",
        token=webhook_service.generate_secret(),
        encrypted_secret=webhook_service.store_secret("original"),
        config={},
    )
    db.add(trigger)
    db.commit()
    db.refresh(trigger)

    with caplog.at_level(logging.INFO, logger="app.routers.triggers"):
        resp = client.post(
            f"{V1}/triggers/{trigger.id}/rotate-secret", headers=auth
        )
    assert resp.status_code == 200

    lines = _lines(caplog, "app.routers.triggers", logging.INFO)
    assert any(
        f"trigger {trigger.id}" in line and "rotated" in line
        for line in lines
    )


# --------------------------------------------------------------------------- #
# 7. Rotation logging — API keys                                              #
# --------------------------------------------------------------------------- #


def test_api_key_rotation_with_immediate_revoke_is_logged(
    client, db, auth, user, project, caplog
):
    key, _token = api_key_service.mint(
        db,
        project=project,
        name="rotate-me",
        scopes=[ApiKeyScope.CONTENT_READ],
    )
    db.commit()
    db.refresh(key)

    with caplog.at_level(logging.INFO, logger="app.services.api_keys"):
        resp = client.post(
            f"{V1}/api-keys/{key.id}/rotate",
            headers=auth,
            json={"grace_hours": 0},
        )
    assert resp.status_code == 200

    lines = _lines(caplog, "app.services.api_keys", logging.INFO)
    assert any(
        key.prefix in line and "rotated" in line
        for line in lines
    )


def test_api_key_rotation_with_grace_period_is_logged(
    client, db, auth, user, project, caplog
):
    key, _token = api_key_service.mint(
        db,
        project=project,
        name="grace-rotate",
        scopes=[ApiKeyScope.CONTENT_READ],
    )
    db.commit()
    db.refresh(key)

    with caplog.at_level(logging.INFO, logger="app.services.api_keys"):
        resp = client.post(
            f"{V1}/api-keys/{key.id}/rotate",
            headers=auth,
            json={"grace_hours": 24},
        )
    assert resp.status_code == 200

    lines = _lines(caplog, "app.services.api_keys", logging.INFO)
    assert any(
        key.prefix in line and "rotated" in line and "24h grace" in line
        for line in lines
    )
