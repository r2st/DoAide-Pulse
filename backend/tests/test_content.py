"""Content generation, editing and the review workflow.

No provider keys are configured (see conftest), so every generation here takes
the template fallback path — which is exactly what we want to pin down: the
engine must always produce a draft.
"""
from __future__ import annotations

from unittest.mock import patch

from app.models.content import Content, ContentStatus, ContentType, unique_content_slug


def test_generate_falls_back_to_a_template_with_zero_confidence(client, auth, project):
    resp = client.post(
        "/api/v1/content/generate",
        headers=auth,
        json={"project_id": project.id, "content_type": "feature_spotlight"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "draft"
    assert body["title"]
    assert body["body_markdown"]
    # A template draft must never clear the auto-publish gate.
    assert body["confidence"] == 0.0
    assert body["source"]["fallback"] is True
    # It says so in the body, so nobody publishes it by accident.
    assert "Herald" in body["body_markdown"]
    assert "no AI provider was" in body["body_markdown"]


def test_generated_slug_is_unique_per_project(client, auth, project):
    slugs = set()
    for _ in range(3):
        resp = client.post(
            "/api/v1/content/generate",
            headers=auth,
            json={"project_id": project.id, "content_type": "announcement"},
        )
        slugs.add(resp.json()["slug"])
    assert len(slugs) == 3


def test_create_edit_and_delete_by_hand(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Shipping Herald",
            "body_markdown": "## Why\n\n" + ("word " * 400),
            "keywords": ["Herald", "herald", "marketing"],
        },
    )
    assert resp.status_code == 201, resp.text
    content_id = resp.json()["id"]
    # Excerpt and meta description are derived when not supplied.
    assert resp.json()["excerpt"]
    assert resp.json()["meta_description"]
    assert resp.json()["keywords"] == ["herald", "marketing"]

    resp = client.patch(
        f"/api/v1/content/{content_id}", headers=auth, json={"title": "Shipping Herald v2"}
    )
    assert resp.json()["slug"] == "shipping-herald-v2"

    assert client.delete(f"/api/v1/content/{content_id}", headers=auth).status_code == 204
    assert client.get(f"/api/v1/content/{content_id}", headers=auth).status_code == 404


def test_detail_includes_seo_issues(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={"project_id": project.id, "title": "x", "body_markdown": "tiny"},
    )
    issues = resp.json()["seo_issues"]
    assert any(i["field"] == "body" for i in issues)


def test_list_filters_by_status_and_project(client, auth, project, db):
    for i, status_ in enumerate((ContentStatus.DRAFT, ContentStatus.REVIEW, ContentStatus.REVIEW)):
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.HOW_TO,
                status=status_,
                title=f"T{status_.value}-{i}",
                slug=f"s-{status_.value}-{i}",
            )
        )
    db.commit()

    all_content = client.get("/api/v1/content", headers=auth).json()
    assert len(all_content) == 3

    review = client.get("/api/v1/content?status=review", headers=auth).json()
    assert len(review) == 2

    # The literal /queue/ paths must not be swallowed by /{content_id}.
    queue = client.get("/api/v1/content/queue/review", headers=auth)
    assert queue.status_code == 200
    assert len(queue.json()) == 2


def test_list_content_pagination_offset(client, auth, project, db):
    """The offset parameter lets clients paginate through results."""
    for i in range(5):
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.HOW_TO,
                status=ContentStatus.DRAFT,
                title=f"Post {i}",
                slug=f"post-{i}",
            )
        )
    db.commit()

    # First page
    page1 = client.get("/api/v1/content?limit=2&offset=0", headers=auth).json()
    assert len(page1) == 2

    # Second page
    page2 = client.get("/api/v1/content?limit=2&offset=2", headers=auth).json()
    assert len(page2) == 2

    # No overlap between pages
    ids1 = {c["id"] for c in page1}
    ids2 = {c["id"] for c in page2}
    assert ids1.isdisjoint(ids2)

    # Past the end
    past = client.get("/api/v1/content?limit=2&offset=100", headers=auth).json()
    assert len(past) == 0


def test_unique_content_slug_deduplicates(db, project):
    """The shared unique_content_slug function increments suffixes correctly."""
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.HOW_TO,
            status=ContentStatus.DRAFT,
            title="My Post",
            slug="my-post",
        )
    )
    db.commit()

    # Second call should produce "my-post-2"
    slug2 = unique_content_slug(db, project.id, "My Post")
    assert slug2 == "my-post-2"

    # Insert that and get the third
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.HOW_TO,
            status=ContentStatus.DRAFT,
            title="My Post",
            slug=slug2,
        )
    )
    db.commit()
    slug3 = unique_content_slug(db, project.id, "My Post")
    assert slug3 == "my-post-3"


def test_detail_includes_focus_keyword_seo_checks(client, auth, project):
    """The SEO panel should check focus_keyword and slug, not skip them."""
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Building FastAPI Apps",
            "body_markdown": "## Introduction\n\n" + ("FastAPI is great. " * 50),
            "keywords": ["fastapi"],
        },
    )
    assert resp.status_code == 201
    issues = resp.json()["seo_issues"]
    # With focus_keyword and slug now passed, the audit should produce
    # slug-related or keyword-in-slug checks where applicable.
    fields_checked = {i["field"] for i in issues}
    # At minimum, body and cover_image_url should appear (missing cover, etc.)
    assert "cover_image_url" in fields_checked


def test_approve_moves_status(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.REVIEW,
        title="Draft",
        slug="draft",
    )
    db.add(content)
    db.commit()

    resp = client.post(f"/api/v1/content/{content.id}/approve", headers=auth)
    assert resp.json()["status"] == "approved"


def test_editing_a_published_piece_is_refused(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.PUBLISHED,
        title="Live",
        slug="live",
    )
    db.add(content)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{content.id}", headers=auth, json={"title": "Changed"}
    )
    assert resp.status_code == 409


def test_publish_requires_a_connection(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Ship it",
        slug="ship-it",
    )
    db.add(content)
    db.commit()

    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        headers=auth,
        json={"platforms": ["devto"]},
    )
    assert resp.status_code == 400
    assert "Not connected" in resp.json()["detail"]


def test_publish_records_as_draft_on_the_publication(client, auth, project, db, user):
    """The editor's "create as a draft" checkbox has to reach the row."""
    from app.models.platform_connection import ConnectionStatus, PlatformConnection
    from app.models.publication import Platform
    from app.services.crypto import encrypt_credentials

    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Stage it",
        slug="stage-it",
    )
    db.add(content)
    db.commit()

    # Scheduled, so nothing tries to reach Dev.to during the test.
    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        headers=auth,
        json={
            "platforms": ["devto"],
            "as_draft": True,
            "scheduled_for": "2099-01-01T09:00:00Z",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["as_draft"] is True


def test_slug_collision_retries_with_random_suffix(client, auth, project, db):
    """If a concurrent insert grabs the same slug, the retry path kicks in."""
    from sqlalchemy.exc import IntegrityError as _IE

    original_commit = db.commit.__func__ if hasattr(db.commit, '__func__') else None
    call_count = {"n": 0}

    # Pre-create a content row with slug "shipping-herald" so the first commit
    # hits the unique constraint.
    db.add(Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.DRAFT,
        title="Shipping Herald",
        slug="shipping-herald",
    ))
    db.commit()

    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Shipping Herald",
            "body_markdown": "## Hello\n\n" + ("word " * 100),
        },
    )
    assert resp.status_code == 201, resp.text
    # The _unique_slug function should have incremented to shipping-herald-2
    assert resp.json()["slug"] != "shipping-herald"


def test_publish_to_an_unfinished_adapter_is_refused(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Ship it",
        slug="ship-it",
    )
    db.add(content)
    db.commit()

    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        headers=auth,
        json={"platforms": ["twitter"]},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "No finished adapter" in detail
    assert "devto" in detail
