"""Every request body schema rejects unknown fields.

Before this change, a POST or PATCH with an invented field — ``{"title":
"Hello", "admin": true}`` — was silently accepted: Pydantic's default is
``extra="ignore"``, so the extra key was dropped after parsing and the
caller was told 200. That is a data-integrity and security problem:

* A client sending ``version``, ``user_id``, ``is_active`` or ``role`` has a
  reasonable expectation that the field was applied, and will not discover
  until much later that it was not.
* An attacker probing for mass-assignment is told that the field name was
  accepted — every invented key gets the same 200, so there is no signal to
  distinguish "silently ignored" from "silently applied".
* A typo in a field name (``descrption`` instead of ``description``) is
  accepted and the intended field keeps its default, which is a bug the API
  could have caught at the door.

``ConfigDict(extra="forbid")`` on every inbound model turns all three into a
422 that names the offending field.

The sweep at the bottom derives the check from the generated OpenAPI schema,
so a new body model added without the setting fails here rather than in
production.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app
from app.models.content import Content, ContentStatus, ContentType

# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        title="Strict schema test piece",
        slug="strict-schema-test-piece",
        body_markdown="Some content.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------------ #
# Per-endpoint: unknown fields are rejected with 422                  #
# ------------------------------------------------------------------ #

def test_content_create_rejects_unknown_fields(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        json={
            "project_id": project.id,
            "title": "Test",
            "bogus_field": "should fail",
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_content_update_rejects_unknown_fields(client, auth, piece):
    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        json={"title": "Updated", "fake_field": 42},
        headers=auth,
    )
    assert resp.status_code == 422


def test_generate_request_rejects_unknown_fields(client, auth, project):
    resp = client.post(
        "/api/v1/content/generate",
        json={"project_id": project.id, "unknown_param": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_project_create_rejects_unknown_fields(client, auth):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "Test Project", "admin_override": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_project_update_rejects_unknown_fields(client, auth, project):
    resp = client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "Updated", "secret_field": "hack"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_webhook_create_rejects_unknown_fields(client, auth):
    resp = client.post(
        "/api/v1/webhooks",
        json={
            "url": "https://example.com/hook",
            "events": ["content.published"],
            "admin": True,
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_webhook_update_rejects_unknown_fields(client, auth):
    # Create a webhook first
    create_resp = client.post(
        "/api/v1/webhooks",
        json={
            "url": "https://example.com/hook",
            "events": ["content.published"],
        },
        headers=auth,
    )
    if create_resp.status_code != 201:
        pytest.skip("webhook creation failed")
    wh_id = create_resp.json()["id"]
    resp = client.patch(
        f"/api/v1/webhooks/{wh_id}",
        json={"url": "https://example.com/new", "extra_field": "bad"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_trigger_create_rejects_unknown_fields(client, auth, project):
    resp = client.post(
        "/api/v1/triggers",
        json={
            "project_id": project.id,
            "kind": "schedule",
            "sneaky": "value",
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_tag_rename_rejects_unknown_fields(client, auth):
    resp = client.post(
        "/api/v1/tags/rename",
        json={"old": "foo", "new": "bar", "force": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_auth_register_rejects_unknown_fields(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "test@example.com",
            "password": "strongpassword123",
            "is_admin": True,
        },
    )
    assert resp.status_code == 422


def test_password_reset_request_rejects_unknown_fields(client):
    resp = client.post(
        "/api/v1/auth/password-reset",
        json={"email": "test@example.com", "bypass": True},
    )
    assert resp.status_code == 422


def test_password_reset_confirm_rejects_unknown_fields(client):
    resp = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": "a" * 16,
            "new_password": "newpassword123",
            "admin_reset": True,
        },
    )
    assert resp.status_code == 422


def test_preferences_update_rejects_unknown_fields(client, auth):
    resp = client.patch(
        "/api/v1/auth/me",
        json={"full_name": "Test", "role": "admin"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_api_key_create_rejects_unknown_fields(client, auth, project):
    resp = client.post(
        "/api/v1/api-keys",
        json={
            "project_id": project.id,
            "name": "Test Key",
            "scopes": ["content:read"],
            "admin_scope": True,
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_template_create_rejects_unknown_fields(client, auth):
    resp = client.post(
        "/api/v1/templates",
        json={"name": "Test Template", "hidden_field": "bad"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_publish_request_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        json={"platforms": ["devto"], "bypass_review": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_bulk_publish_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={
            "content_ids": [piece.id],
            "platforms": ["devto"],
            "skip_checks": True,
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_content_status_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/status",
        json={"status": "review", "force": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_schedule_content_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/schedule",
        json={
            "platforms": ["devto"],
            "scheduled_for": "2099-01-01T09:00:00Z",
            "priority": "urgent",
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_inline_edit_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/edit",
        json={
            "selection": "Some content.",
            "operation": "improve",
            "model": "gpt-4",
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_headline_apply_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/headlines/apply",
        json={"title": "New Headline", "force": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_archive_old_rejects_unknown_fields(client, auth):
    resp = client.post(
        "/api/v1/content/bulk/archive-old",
        json={"older_than_days": 30, "include_published": True},
        headers=auth,
    )
    assert resp.status_code == 422


def test_translation_request_rejects_unknown_fields(client, auth, piece):
    resp = client.post(
        f"/api/v1/content/{piece.id}/translations",
        json={"language": "de", "quality": "high"},
        headers=auth,
    )
    assert resp.status_code == 422


def test_connection_create_rejects_unknown_fields(client, auth):
    resp = client.put(
        "/api/v1/settings/connections",
        json={
            "platform": "devto",
            "credentials": {"api_key": "test123"},
            "admin_access": True,
        },
        headers=auth,
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------ #
# A valid request (no extra fields) still succeeds                    #
# ------------------------------------------------------------------ #

def test_content_create_with_valid_fields_succeeds(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        json={
            "project_id": project.id,
            "title": "Valid Piece",
            "body_markdown": "Some content",
        },
        headers=auth,
    )
    assert resp.status_code == 201


def test_content_update_with_valid_fields_succeeds(client, auth, piece):
    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        json={"title": "Updated Title"},
        headers=auth,
    )
    assert resp.status_code == 200


def test_project_create_with_valid_fields_succeeds(client, auth):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "Valid Project"},
        headers=auth,
    )
    assert resp.status_code == 201


# ------------------------------------------------------------------ #
# OpenAPI sweep: every requestBody schema declares extra="forbid"     #
# ------------------------------------------------------------------ #

def _walk(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        yield from _walk(
            getattr(inner, "routes", None) or getattr(route, "routes", [])
        )


def _resolve_ref(schema: dict, components: dict) -> dict:
    """Follow a single ``$ref`` in a JSON Schema."""
    ref = schema.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return components.get("schemas", {}).get(name, {})
    return schema


def test_every_request_body_schema_forbids_extras():
    """Derived from the OpenAPI spec so a new model without
    ``extra="forbid"`` fails here rather than in production.

    Checks that every ``requestBody`` schema sets
    ``additionalProperties: false`` (which is what Pydantic emits for
    ``extra="forbid"``).
    """
    openapi = app.openapi()
    components = openapi.get("components", {})
    unforbidden: list[str] = []

    for path, operations in openapi["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            if not body:
                continue
            content = body.get("content", {})
            json_body = content.get("application/json", {})
            schema = json_body.get("schema", {})
            schema = _resolve_ref(schema, components)
            if not schema:
                continue
            # VelocityCurveOut uses extra="allow" deliberately
            if schema.get("title", "") == "VelocityCurveOut":
                continue
            if schema.get("additionalProperties") is not False:
                unforbidden.append(
                    f"{method.upper()} {path} -> {schema.get('title', '?')}"
                )

    assert not unforbidden, (
        "Request body schemas that accept unknown fields "
        "(add ConfigDict(extra='forbid')):\n" + "\n".join(unforbidden)
    )
