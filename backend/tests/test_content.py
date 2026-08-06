"""Content generation, editing and the review workflow.

No provider keys are configured (see conftest), so every generation here takes
the template fallback path — which is exactly what we want to pin down: the
engine must always produce a draft.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.content import Content, ContentStatus, ContentType, unique_content_slug


def _soon() -> str:
    """A publish time far enough ahead that nothing goes out during the test.

    Not a far-future sentinel: scheduling refuses anything past a one-year
    horizon, so that a mistyped year cannot park a post for a decade.
    """
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


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


def test_create_content_sets_focus_keyword_from_first_keyword(client, auth, project):
    """When no explicit focus_keyword is given, the first keyword is used."""
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Herald Marketing",
            "body_markdown": "## Hi\n\n" + ("word " * 100),
            "keywords": ["marketing automation", "developer tools"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["focus_keyword"] == "marketing automation"


def test_create_content_explicit_focus_keyword(client, auth, project):
    """An explicit focus_keyword overrides the default."""
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Herald Marketing",
            "body_markdown": "## Hi\n\n" + ("word " * 100),
            "keywords": ["marketing automation", "developer tools"],
            "focus_keyword": "developer tools",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["focus_keyword"] == "developer tools"


def test_generate_content_sets_focus_keyword(client, auth, project):
    """The generate endpoint must propagate focus_keyword from the engine."""
    resp = client.post(
        "/api/v1/content/generate",
        headers=auth,
        json={"project_id": project.id, "content_type": "announcement"},
    )
    assert resp.status_code == 201
    body = resp.json()
    # The template fallback uses the first project keyword as focus_keyword.
    assert body["focus_keyword"] == "marketing automation"


def test_update_keywords_syncs_focus_keyword(client, auth, project, db):
    """Changing keywords should update focus_keyword to the new first keyword."""
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.HOW_TO,
            status=ContentStatus.DRAFT,
            title="Test",
            slug="test-sync",
            focus_keyword="old",
        )
    )
    db.commit()
    content_id = db.query(Content).filter(Content.slug == "test-sync").first().id

    resp = client.patch(
        f"/api/v1/content/{content_id}",
        headers=auth,
        json={"keywords": ["new primary", "secondary"]},
    )
    assert resp.status_code == 200
    assert resp.json()["focus_keyword"] == "new primary"


def test_list_content_total_count_header(client, auth, project, db):
    """The X-Total-Count header reports the full count before pagination."""
    for i in range(7):
        db.add(
            Content(
                project_id=project.id,
                content_type=ContentType.HOW_TO,
                status=ContentStatus.DRAFT,
                title=f"Post {i}",
                slug=f"total-{i}",
            )
        )
    db.commit()

    resp = client.get("/api/v1/content?limit=3&offset=0", headers=auth)
    assert resp.status_code == 200
    assert len(resp.json()) == 3
    assert resp.headers["X-Total-Count"] == "7"

    # Filtered count should only count matching rows.
    resp2 = client.get("/api/v1/content?limit=100&status=review", headers=auth)
    assert resp2.headers["X-Total-Count"] == "0"


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
            "scheduled_for": _soon(),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["as_draft"] is True


def test_a_taken_slug_is_incremented_before_it_is_ever_inserted(client, auth, project, db):
    """The ordinary collision never reaches the database.

    ``unique_content_slug`` queries for the base slug first, so a second piece
    with the same title is numbered rather than rejected. This is the common
    path; the constraint below it only catches the race.
    """
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
    assert resp.json()["slug"] == "shipping-herald-2"


def test_a_slug_that_is_taken_between_the_check_and_the_insert_still_lands(
    client, auth, project, db, monkeypatch
):
    """The race the pre-check cannot win, and the retry that covers it.

    Two concurrent requests can both read "shipping-herald" as free and both
    try to insert it; the unique constraint catches the loser. Forcing the
    pre-check to hand back a slug that is already taken reproduces exactly that
    state, and the retry has to turn a would-be 500 into a created row.
    """
    db.add(Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.DRAFT,
        title="Shipping Herald",
        slug="shipping-herald",
    ))
    db.commit()

    # Stand in for the concurrent insert: the check reports the taken slug as
    # free, so the commit below is the one that discovers otherwise.
    monkeypatch.setattr(
        "app.routers.content.unique_content_slug",
        lambda db, project_id, title: "shipping-herald",
    )

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
    slug = resp.json()["slug"]
    # A random suffix rather than a number: the retry cannot re-run the count
    # query without risking the same race a second time.
    assert slug.startswith("shipping-herald-")
    assert slug != "shipping-herald"
    # And it is a real row, not an uncommitted object that happens to serialise.
    assert db.scalar(
        select(Content.id).where(Content.project_id == project.id, Content.slug == slug)
    )


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


# --------------------------------------------------------------------------- #
# Internal link suggestions                                                   #
# --------------------------------------------------------------------------- #


def test_internal_links_suggests_published_posts_by_shared_keyword(
    client, auth, project, db
):
    target = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.DRAFT,
        title="New piece",
        slug="new-piece",
        keywords=["celery", "retries"],
    )
    match = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Celery deep dive",
        slug="celery-deep-dive",
        keywords=["celery", "async"],
        canonical_url="https://example.com/celery-deep-dive",
    )
    unpublished_match = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        title="Draft about celery",
        slug="draft-celery",
        keywords=["celery"],
    )
    no_overlap = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Totally different",
        slug="totally-different",
        keywords=["docker"],
    )
    db.add_all([target, match, unpublished_match, no_overlap])
    db.commit()

    resp = client.get(
        f"/api/v1/content/{target.id}/internal-links", headers=auth
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["content_id"] for s in body] == [match.id]
    assert body[0]["matched_keywords"] == ["celery"]
    assert body[0]["url"] == "https://example.com/celery-deep-dive"


def test_internal_links_empty_when_no_keyword_overlap(client, auth, project, db):
    target = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="No keywords here",
        slug="no-keywords-here",
    )
    db.add(target)
    db.commit()

    resp = client.get(f"/api/v1/content/{target.id}/internal-links", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# Repurposing                                                                  #
# --------------------------------------------------------------------------- #


def test_repurpose_returns_snippets_via_the_mechanical_fallback(client, auth, project, db):
    """No provider key is configured in tests, so this exercises the fallback."""
    body = (
        "## Section one\n\n"
        + ("A concrete sentence about the feature. " * 15)
        + "\n\n## Section two\n\n"
        + ("Another concrete sentence about the feature. " * 15)
    )
    content = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="Herald ships bulk content operations",
        slug="herald-ships-bulk-content-operations",
        body_markdown=body,
        excerpt="Approve, reject or publish many drafts in one call.",
        tags=["python", "fastapi"],
    )
    db.add(content)
    db.commit()

    resp = client.post(f"/api/v1/content/{content.id}/repurpose", headers=auth)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["is_fallback"] is True
    assert payload["provider"] is None
    assert 1 <= len(payload["twitter_thread"]) <= 5
    assert payload["linkedin_post"]


def test_repurpose_404s_for_content_owned_by_someone_else(client, auth, db):
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    from app.models.project import Project, Tone

    other_project = Project(
        user_id=other.id, name="Other", slug="other", tone=Tone.TECHNICAL
    )
    db.add(other_project)
    db.commit()
    other_content = Content(
        project_id=other_project.id,
        content_type=ContentType.HOW_TO,
        title="Not yours",
        slug="not-yours",
        body_markdown="word " * 100,
    )
    db.add(other_content)
    db.commit()

    resp = client.post(f"/api/v1/content/{other_content.id}/repurpose", headers=auth)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Headline testing                                                            #
# --------------------------------------------------------------------------- #


def test_headline_variants_endpoint_uses_fallback_without_a_provider(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="Herald ships bulk content operations",
        slug="bulk-ops",
    )
    db.add(content)
    db.commit()

    resp = client.post(f"/api/v1/content/{content.id}/headlines", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_fallback"] is True
    assert body["variants"]
    assert content.title not in body["variants"]


def test_apply_headline_updates_title_and_keeps_slug_even_when_published(client, auth, project, db):
    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="Original title",
        slug="original-title",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.commit()

    resp = client.post(
        f"/api/v1/content/{content.id}/headlines/apply",
        headers=auth,
        json={"title": "A Better Headline"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "A Better Headline"
    # The slug must not move — it's the URL this piece is already live at.
    assert body["slug"] == "original-title"

    db.refresh(content)
    assert len(content.headline_history) == 1
    assert content.headline_history[0]["title"] == "Original title"


def test_apply_headline_404s_for_content_owned_by_someone_else(client, auth, db):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other2@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    other_project = Project(user_id=other.id, name="Other", slug="other2", tone=Tone.TECHNICAL)
    db.add(other_project)
    db.commit()
    other_content = Content(
        project_id=other_project.id,
        content_type=ContentType.HOW_TO,
        title="Not yours",
        slug="not-yours-2",
    )
    db.add(other_content)
    db.commit()

    resp = client.post(
        f"/api/v1/content/{other_content.id}/headlines/apply",
        headers=auth,
        json={"title": "Hijacked"},
    )
    assert resp.status_code == 404


def test_headline_performance_endpoint_returns_current_window_by_default(client, auth, project, db):
    content = Content(
        project_id=project.id, content_type=ContentType.HOW_TO, title="A Title", slug="a-title",
    )
    db.add(content)
    db.commit()

    resp = client.get(f"/api/v1/content/{content.id}/headlines/performance", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["current"] is True
    assert body[0]["title"] == "A Title"
    assert body[0]["snapshots"] == 0


# --------------------------------------------------------------------------- #
# Bulk operations                                                             #
# --------------------------------------------------------------------------- #


def test_bulk_approve_moves_status_and_reports_failures(client, auth, project, db):
    a = Content(
        project_id=project.id, content_type=ContentType.HOW_TO,
        status=ContentStatus.REVIEW, title="A", slug="a",
    )
    already_published = Content(
        project_id=project.id, content_type=ContentType.HOW_TO,
        status=ContentStatus.PUBLISHED, title="B", slug="b",
    )
    db.add_all([a, already_published])
    db.commit()

    resp = client.post(
        "/api/v1/content/bulk/approve",
        headers=auth,
        json={"content_ids": [a.id, already_published.id, 999999]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == [a.id]
    failed_ids = {f["content_id"] for f in body["failed"]}
    assert failed_ids == {already_published.id, 999999}

    db.refresh(a)
    assert a.status == ContentStatus.APPROVED


def test_bulk_reject_archives_content(client, auth, project, db):
    a = Content(
        project_id=project.id, content_type=ContentType.HOW_TO,
        status=ContentStatus.REVIEW, title="A", slug="a",
    )
    db.add(a)
    db.commit()

    resp = client.post(
        "/api/v1/content/bulk/reject", headers=auth, json={"content_ids": [a.id]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["succeeded"] == [a.id]

    db.refresh(a)
    assert a.status == ContentStatus.ARCHIVED


def test_bulk_operations_ignore_content_owned_by_another_user(client, auth, db):
    """A bulk call must not leak the existence of someone else's content."""
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    other_user = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other_user)
    db.commit()
    other_project = Project(
        user_id=other_user.id,
        name="Other",
        slug="other",
        description="Someone else's project.",
        tone=Tone.TECHNICAL,
    )
    db.add(other_project)
    db.commit()
    theirs = Content(
        project_id=other_project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.REVIEW,
        title="Not yours",
        slug="not-yours",
    )
    db.add(theirs)
    db.commit()

    resp = client.post(
        "/api/v1/content/bulk/approve", headers=auth, json={"content_ids": [theirs.id]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == []
    assert body["failed"][0]["reason"] == "Not found"

    db.refresh(theirs)
    assert theirs.status == ContentStatus.REVIEW


def test_bulk_publish_requires_connection_per_item(client, auth, project, db):
    connected = Content(
        project_id=project.id, content_type=ContentType.ANNOUNCEMENT,
        title="Ship it", slug="ship-it-bulk",
    )
    db.add(connected)
    db.commit()

    resp = client.post(
        "/api/v1/content/bulk/publish",
        headers=auth,
        json={"content_ids": [connected.id], "platforms": ["devto"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == []
    assert "Not connected" in body["failed"][0]["reason"]


def test_bulk_publish_succeeds_for_connected_platform(client, auth, project, db, user):
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
        project_id=project.id, content_type=ContentType.ANNOUNCEMENT,
        title="Stage it", slug="stage-it-bulk",
    )
    db.add(content)
    db.commit()

    resp = client.post(
        "/api/v1/content/bulk/publish",
        headers=auth,
        json={
            "content_ids": [content.id],
            "platforms": ["devto"],
            "as_draft": True,
            "scheduled_for": _soon(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == [content.id]
    assert body["failed"] == []

    db.refresh(content)
    assert content.status == ContentStatus.APPROVED
