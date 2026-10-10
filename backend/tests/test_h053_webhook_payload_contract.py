"""The webhook payload shapes are a published contract.

:mod:`app.services.webhook_payloads` builds the dicts Pulse puts in a webhook
body. A receiver that parses ``content.title`` or ``publication.platform``
breaks if the key moves. These tests pin the contract: every expected key
is present, the types are right, and ``None`` appears only where the spec says
it can.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import webhook_payloads


def _content(db, project, *, published_at=None) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        status=ContentStatus.PUBLISHED,
        title="Test Post",
        slug="test-post",
        body_markdown="# Test\n\nBody.",
        excerpt="Excerpt.",
        meta_description="Meta.",
        keywords=["test"],
        word_count=42,
        published_at=published_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(db, content) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_url="https://dev.to/x/test-post",
        attempts=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_content_payload_keys(db, project):
    content = _content(db, project)
    payload = webhook_payloads.content_payload(content)

    expected_keys = {
        "id", "title", "slug", "content_type", "status",
        "canonical_url", "excerpt", "word_count", "published_at", "project",
    }
    assert set(payload.keys()) == expected_keys


def test_content_payload_values_are_serializable(db, project):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    content = _content(db, project, published_at=ts)
    payload = webhook_payloads.content_payload(content)

    assert payload["id"] == content.id
    assert payload["title"] == "Test Post"
    assert payload["slug"] == "test-post"
    assert payload["content_type"] == "feature_spotlight"
    assert payload["status"] == "published"
    assert payload["word_count"] == 42
    assert payload["published_at"] is not None
    assert "2026-07-01" in payload["published_at"]


def test_content_payload_project_shape(db, project):
    content = _content(db, project)
    proj = webhook_payloads.content_payload(content)["project"]

    assert proj["id"] == project.id
    assert proj["name"] == project.name
    assert proj["slug"] == project.slug


def test_content_payload_null_published_at(db, project):
    content = _content(db, project, published_at=None)
    assert webhook_payloads.content_payload(content)["published_at"] is None


def test_publication_payload_keys(db, project):
    content = _content(db, project)
    pub = _publication(db, content)
    payload = webhook_payloads.publication_payload(pub)

    expected_keys = {"id", "platform", "status", "external_url", "attempts", "error"}
    assert set(payload.keys()) == expected_keys


def test_publication_payload_values(db, project):
    content = _content(db, project)
    pub = _publication(db, content)
    payload = webhook_payloads.publication_payload(pub)

    assert payload["platform"] == "devto"
    assert payload["status"] == "published"
    assert payload["external_url"] == "https://dev.to/x/test-post"
    assert payload["attempts"] == 1
    assert payload["error"] is None


def test_engagement_payload_wraps_content_and_numbers(db, project):
    content = _content(db, project)
    payload = webhook_payloads.engagement_payload(
        content, threshold=100, engagement=150, views=500, platforms=["devto", "medium"],
    )

    assert set(payload.keys()) == {"content", "threshold", "engagement", "views", "platforms"}
    assert payload["threshold"] == 100
    assert payload["engagement"] == 150
    assert payload["views"] == 500
    assert payload["platforms"] == ["devto", "medium"]
    assert payload["content"]["id"] == content.id
