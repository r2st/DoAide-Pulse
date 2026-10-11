"""Tests for viral sharing features: public articles, subscribers, content ideas."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project
from app.models.subscriber import Subscriber


@pytest.fixture
def project(db, user):
    p = Project(
        user_id=user.id,
        name="Test Project",
        slug="test-project",
        repo_url="https://github.com/test/test",
        description="A test project",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def published_content(db, project):
    from app.models.mixins import utcnow

    c = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Test Article Title",
        slug="test-article-title",
        body_markdown="# Hello\n\nThis is a test article.",
        excerpt="A test article excerpt",
        meta_description="A meta description for testing",
        tags=["python", "testing"],
        published_at=utcnow(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class TestPublicArticle:
    def test_returns_published_article_by_slug(self, client, published_content):
        resp = client.get(f"/api/v1/articles/{published_content.slug}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test Article Title"
        assert data["slug"] == "test-article-title"
        assert data["excerpt"] == "A test article excerpt"
        assert data["tags"] == ["python", "testing"]
        assert data["project_name"] == "Test Project"
        assert "body_markdown" in data

    def test_404_for_nonexistent_slug(self, client, db):
        resp = client.get("/api/v1/articles/does-not-exist")
        assert resp.status_code == 404

    def test_draft_is_not_public(self, client, db, project):
        c = Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.DRAFT,
            title="Draft Article",
            slug="draft-article",
            body_markdown="Draft body",
        )
        db.add(c)
        db.commit()
        resp = client.get("/api/v1/articles/draft-article")
        assert resp.status_code == 404


class TestSubscriber:
    def test_subscribe_with_valid_email(self, client, db):
        resp = client.post(
            "/api/v1/subscribers",
            json={"email": "reader@example.com"},
        )
        assert resp.status_code == 201
        assert resp.json()["ok"] is True
        sub = db.query(Subscriber).filter_by(email="reader@example.com").first()
        assert sub is not None
        assert sub.source == "embed"

    def test_subscribe_with_custom_source(self, client, db):
        resp = client.post(
            "/api/v1/subscribers",
            json={"email": "reader2@example.com", "source": "article-cta"},
        )
        assert resp.status_code == 201
        sub = db.query(Subscriber).filter_by(email="reader2@example.com").first()
        assert sub.source == "article-cta"

    def test_duplicate_email_does_not_error(self, client, db):
        client.post("/api/v1/subscribers", json={"email": "dup@example.com"})
        resp = client.post("/api/v1/subscribers", json={"email": "dup@example.com"})
        assert resp.status_code == 201

    def test_invalid_email_rejected(self, client, db):
        resp = client.post(
            "/api/v1/subscribers",
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 422

    def test_email_is_lowercased(self, client, db):
        client.post("/api/v1/subscribers", json={"email": "UPPER@Example.COM"})
        sub = db.query(Subscriber).filter_by(email="upper@example.com").first()
        assert sub is not None


class TestContentIdeaGenerator:
    def _mock_gemini_response(self, ideas):
        import json

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(ideas),
                    }
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    @patch("app.routers.viral.settings")
    @patch("app.routers.viral.httpx.post")
    def test_generates_ideas_for_niche(self, mock_post, mock_settings, client):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.gemini_base_url = "https://test.example.com/v1beta/openai/"
        mock_settings.rate_limit_public_read = "100/minute"
        ideas = [
            {"title": "Idea 1", "hook": "Hook 1", "content_type": "tutorial"},
            {"title": "Idea 2", "hook": "Hook 2", "content_type": "how_to"},
        ]
        mock_post.return_value = self._mock_gemini_response(ideas)

        resp = client.post(
            "/api/v1/tools/content-ideas",
            json={"niche": "AI tools", "count": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["niche"] == "AI tools"
        assert len(data["ideas"]) == 2
        assert data["ideas"][0]["title"] == "Idea 1"

    @patch("app.routers.viral.settings")
    def test_503_when_no_api_key(self, mock_settings, client):
        mock_settings.gemini_api_key = ""
        mock_settings.rate_limit_public_read = "100/minute"
        resp = client.post(
            "/api/v1/tools/content-ideas",
            json={"niche": "fitness"},
        )
        assert resp.status_code == 503

    def test_rejects_empty_niche(self, client):
        resp = client.post(
            "/api/v1/tools/content-ideas",
            json={"niche": ""},
        )
        assert resp.status_code == 422

    @patch("app.routers.viral.settings")
    @patch("app.routers.viral.httpx.post")
    def test_uses_configured_model_not_hardcoded(self, mock_post, mock_settings, client):
        mock_settings.gemini_api_key = "test-key"
        mock_settings.gemini_base_url = "https://test.example.com/v1beta/openai/"
        mock_settings.gemini_model = "gemini-flash-latest"
        mock_settings.rate_limit_public_read = "100/minute"
        mock_post.return_value = self._mock_gemini_response(
            [{"title": "X", "hook": "Y", "content_type": "tutorial"}],
        )

        client.post(
            "/api/v1/tools/content-ideas",
            json={"niche": "testing", "count": 1},
        )

        call_kwargs = mock_post.call_args
        sent_model = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))["model"]
        assert sent_model == "gemini-flash-latest", (
            f"Expected configured model 'gemini-flash-latest', got '{sent_model}'"
        )

    def test_rejects_too_many_ideas(self, client):
        resp = client.post(
            "/api/v1/tools/content-ideas",
            json={"niche": "cooking", "count": 50},
        )
        assert resp.status_code == 422
