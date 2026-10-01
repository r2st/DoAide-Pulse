"""Shareable draft preview links.

A signed-nothing, hash-stored, expiring URL that shows one draft to whoever
holds it — no bearer token, no account. The same shape as password reset
tokens (see test_password_reset.py), with the difference that a preview link
is multi-use and several can be live for one draft at once.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.content import Content, ContentType
from app.models.mixins import as_aware, utcnow
from app.models.preview_link import PreviewLink
from app.services import preview_links

PUBLIC = "/api/v1/content/preview"


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Shipping Pulse 1.0",
        slug="shipping-herald-1-0",
        body_markdown="## It shipped\n\n" + ("word " * 50),
        excerpt="Pulse 1.0 shipped today.",
        meta_description="Pulse 1.0 shipped today.",
        cover_image_url="https://example.com/cover.png",
        tags=["python"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _links_url(content_id: int) -> str:
    return f"/api/v1/content/{content_id}/preview-links"


# --------------------------------------------------------------------------- #
# Issuing                                                                      #
# --------------------------------------------------------------------------- #


def test_creating_a_link_returns_a_usable_url(client, auth, content):
    resp = client.post(_links_url(content.id), json={}, headers=auth)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url"].startswith(settings.frontend_url)
    assert "/preview/" in body["url"]
    assert body["view_count"] == 0
    assert body["revoked_at"] is None


def test_the_default_ttl_is_the_configured_one(client, auth, content):
    before = utcnow()
    resp = client.post(_links_url(content.id), json={}, headers=auth)
    expires_at = as_aware(datetime.fromisoformat(resp.json()["expires_at"]))

    delta = expires_at - before
    expected = timedelta(hours=settings.preview_link_default_ttl_hours)
    assert abs(delta - expected) < timedelta(minutes=1)


def test_a_requested_ttl_beyond_the_ceiling_is_capped(client, auth, content):
    resp = client.post(
        _links_url(content.id),
        json={"ttl_hours": settings.preview_link_max_ttl_hours * 10},
        headers=auth,
    )
    expires_at = as_aware(datetime.fromisoformat(resp.json()["expires_at"]))
    delta = expires_at - utcnow()
    assert delta <= timedelta(hours=settings.preview_link_max_ttl_hours) + timedelta(minutes=1)


def test_only_a_hash_of_the_token_is_stored(client, db, auth, content):
    resp = client.post(_links_url(content.id), json={}, headers=auth)
    url = resp.json()["url"]
    token = url.rsplit("/", 1)[-1]

    row = db.scalar(select(PreviewLink))
    assert row is not None
    assert token not in row.token_hash
    assert row.token_hash == preview_links.hash_token(token)
    assert len(row.token_hash) == 64


def test_a_zero_or_negative_ttl_is_rejected(client, auth, content):
    assert client.post(
        _links_url(content.id), json={"ttl_hours": 0}, headers=auth
    ).status_code == 422
    assert client.post(
        _links_url(content.id), json={"ttl_hours": -5}, headers=auth
    ).status_code == 422


def test_a_second_link_does_not_revoke_the_first(client, auth, content):
    """Unlike a password reset token, several reviewers can each hold a live
    link to the same draft."""
    first = client.post(_links_url(content.id), json={}, headers=auth).json()
    client.post(_links_url(content.id), json={}, headers=auth)

    first_token = first["url"].rsplit("/", 1)[-1]
    resp = client.get(f"{PUBLIC}/{first_token}")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Listing and revoking                                                        #
# --------------------------------------------------------------------------- #


def test_listing_does_not_reveal_the_url_again(client, auth, content):
    client.post(_links_url(content.id), json={}, headers=auth)
    resp = client.get(_links_url(content.id), headers=auth)
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 1
    assert listed[0]["url"] is None


def test_revoking_ends_the_link(client, auth, content):
    created = client.post(_links_url(content.id), json={}, headers=auth).json()
    token = created["url"].rsplit("/", 1)[-1]

    resp = client.delete(f"{_links_url(content.id)}/{created['id']}", headers=auth)
    assert resp.status_code == 204

    listed = client.get(_links_url(content.id), headers=auth).json()
    assert listed[0]["revoked_at"] is not None

    assert client.get(f"{PUBLIC}/{token}").status_code == 404


def test_revoking_an_unknown_link_is_a_404(client, auth, content):
    resp = client.delete(f"{_links_url(content.id)}/999999", headers=auth)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The public read                                                             #
# --------------------------------------------------------------------------- #


def test_the_public_read_carries_the_draft_and_nothing_about_its_status(
    client, auth, content
):
    created = client.post(_links_url(content.id), json={}, headers=auth).json()
    token = created["url"].rsplit("/", 1)[-1]

    resp = client.get(f"{PUBLIC}/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == content.title
    assert body["body_markdown"] == content.body_markdown
    assert body["excerpt"] == content.excerpt
    assert body["cover_image_url"] == content.cover_image_url
    assert body["word_count"] == content.word_count
    assert "status" not in body
    assert "generated_by_provider" not in body


def test_an_invented_token_is_a_404(client):
    resp = client.get(f"{PUBLIC}/not-a-real-token-but-long-enough")
    assert resp.status_code == 404


def test_an_expired_link_is_a_404(client, db, auth, content):
    created = client.post(_links_url(content.id), json={}, headers=auth).json()
    token = created["url"].rsplit("/", 1)[-1]

    row = db.scalar(select(PreviewLink))
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    assert client.get(f"{PUBLIC}/{token}").status_code == 404


def test_a_view_is_recorded(client, auth, content):
    created = client.post(_links_url(content.id), json={}, headers=auth).json()
    token = created["url"].rsplit("/", 1)[-1]

    client.get(f"{PUBLIC}/{token}")
    client.get(f"{PUBLIC}/{token}")

    listed = client.get(_links_url(content.id), headers=auth).json()
    assert listed[0]["view_count"] == 2
    assert listed[0]["last_viewed_at"] is not None


def test_a_failed_read_does_not_count_as_a_view(client, auth, content):
    created = client.post(_links_url(content.id), json={}, headers=auth).json()
    token = created["url"].rsplit("/", 1)[-1]
    row_id = created["id"]

    client.delete(f"{_links_url(content.id)}/{row_id}", headers=auth)
    client.get(f"{PUBLIC}/{token}")

    listed = client.get(_links_url(content.id), headers=auth).json()
    assert listed[0]["view_count"] == 0


# --------------------------------------------------------------------------- #
# Ownership                                                                    #
# --------------------------------------------------------------------------- #


def test_creating_a_link_for_someone_elses_content_is_a_404(client, db, auth):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com", full_name="Other", hashed_password=hash_password("x" * 12)
    )
    db.add(other)
    db.commit()
    other_project = Project(
        user_id=other.id, name="Other", slug="other", tone=Tone.TECHNICAL
    )
    db.add(other_project)
    db.commit()
    other_content = Content(
        project_id=other_project.id,
        content_type=ContentType.TUTORIAL,
        title="Not yours",
        slug="not-yours",
        body_markdown="x",
    )
    db.add(other_content)
    db.commit()
    db.refresh(other_content)

    assert client.post(_links_url(other_content.id), json={}, headers=auth).status_code == 404
    assert client.get(_links_url(other_content.id), headers=auth).status_code == 404


def test_deleting_content_deletes_its_preview_links(client, db, auth, content):
    client.post(_links_url(content.id), json={}, headers=auth)
    assert db.scalar(select(PreviewLink)) is not None

    assert client.delete(f"/api/v1/content/{content.id}", headers=auth).status_code == 204
    assert db.scalar(select(PreviewLink)) is None
