"""The social-card endpoint: ownership, and where the card's URL comes from.

The card-building rules themselves are pinned down in test_social_cards.py.
What is only testable through the API is which URL the card ends up carrying —
canonical first, then wherever the piece actually went live — and that one
user cannot read another's.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus


def _make_content(db, project, **overrides) -> Content:
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.DRAFT,
        title=overrides.pop("title", "Shipping Pulse v2"),
        slug=overrides.pop("slug", "shipping-pulse-v2"),
        body_markdown="## Why\n\nA reasonably long opening paragraph about the release.",
        excerpt="What changed in v2.",
        meta_description=overrides.pop(
            "meta_description",
            "Pulse v2 adds learned posting times and social preview cards.",
        ),
        tags=["python"],
        **overrides,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


def test_the_panel_returns_a_preview_for_every_network(client, auth, db, project):
    content = _make_content(db, project, cover_image_url="https://cdn.example.com/c.png")

    resp = client.get(f"/api/v1/content/{content.id}/social", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {p["network"] for p in body["previews"]} == {"x", "linkedin", "facebook", "slack"}
    assert body["recommended_image"] == {"width": 1200, "height": 630}


def test_a_missing_cover_image_is_reported_as_an_error(client, auth, db, project):
    content = _make_content(db, project)

    body = client.get(f"/api/v1/content/{content.id}/social", headers=auth).json()

    assert any(i["field"] == "cover_image_url" and i["level"] == "error" for i in body["issues"])
    assert all(p["card_type"] == "summary" for p in body["previews"])


def test_the_card_uses_the_canonical_url_when_the_piece_names_one(client, auth, db, project):
    content = _make_content(db, project, canonical_url="https://blog.example.com/v2")

    body = client.get(f"/api/v1/content/{content.id}/social", headers=auth).json()

    assert all(p["domain"] == "blog.example.com" for p in body["previews"])


def test_without_a_canonical_the_card_falls_back_to_where_it_went_live(
    client, auth, db, project
):
    content = _make_content(db, project)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            external_url="https://dev.to/pulse/shipping-v2",
            published_at=datetime.now(UTC),
        )
    )
    db.commit()

    body = client.get(f"/api/v1/content/{content.id}/social", headers=auth).json()

    assert all(p["domain"] == "dev.to" for p in body["previews"])


def test_an_unpublished_publication_is_not_treated_as_the_live_url(client, auth, db, project):
    # A scheduled post has no page yet, so its external_url is not an address
    # the card can claim.
    content = _make_content(db, project)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.SCHEDULED,
            external_url="https://dev.to/pulse/not-yet",
        )
    )
    db.commit()

    body = client.get(f"/api/v1/content/{content.id}/social", headers=auth).json()

    assert all(p["domain"] == "" for p in body["previews"])


def test_the_meta_tags_are_pasteable_html(client, auth, db, project):
    content = _make_content(db, project, cover_image_url="https://cdn.example.com/c.png")

    body = client.get(f"/api/v1/content/{content.id}/social", headers=auth).json()

    assert '<meta property="og:title"' in body["meta_html"]
    assert '<meta name="twitter:card" content="summary_large_image">' in body["meta_html"]
    keys = {tag["key"] for tag in body["meta_tags"]}
    assert {"og:title", "og:description", "og:image", "twitter:card"} <= keys


def test_another_users_content_is_not_readable(client, auth, db):
    """404, not 403 — the endpoint must not confirm the piece exists."""
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
    theirs = _make_content(db, other_project, slug="theirs")

    resp = client.get(f"/api/v1/content/{theirs.id}/social", headers=auth)

    assert resp.status_code == 404


def test_the_endpoint_requires_authentication(client, db, project):
    content = _make_content(db, project)

    assert client.get(f"/api/v1/content/{content.id}/social").status_code == 401
