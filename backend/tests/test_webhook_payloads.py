"""The shapes ``app.services.webhooks.emit`` hands to a receiver.

These are a published contract with code Herald cannot see (see the module
docstring), so a field silently dropped or renamed here is a breaking change
nothing catches — every other caller of this module goes through
``content_pipeline`` or ``publishing_service``, which assert on their own
outcome and never look at the payload's actual shape.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import webhook_payloads


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\nword " * 30,
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
        canonical_url="https://herald.example.com/blog/herald-1-0",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_content_payload_carries_the_project_it_belongs_to(content, project):
    payload = webhook_payloads.content_payload(content)

    assert payload["id"] == content.id
    assert payload["title"] == "Herald 1.0"
    assert payload["content_type"] == ContentType.ANNOUNCEMENT.value
    assert payload["status"] == ContentStatus.DRAFT.value
    assert payload["canonical_url"] == content.canonical_url
    assert payload["project"] == {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
    }


def test_content_payload_serializes_an_unpublished_date_as_none(content):
    assert content.published_at is None
    assert webhook_payloads.content_payload(content)["published_at"] is None


def test_publication_payload_carries_the_platform_outcome(db, content):
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        external_url="",
        attempts=3,
        error="Dev.to rejected the credentials",
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    payload = webhook_payloads.publication_payload(publication)
    assert payload == {
        "id": publication.id,
        "platform": Platform.DEVTO.value,
        "status": PublicationStatus.FAILED.value,
        "external_url": "",
        "attempts": 3,
        "error": "Dev.to rejected the credentials",
    }
