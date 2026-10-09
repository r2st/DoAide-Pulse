"""Tests for the uploads and AI field-generation endpoints."""
from __future__ import annotations

import io

import pytest


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestUploadImage:
    def test_upload_png(self, client, auth):
        resp = client.post(
            "/api/v1/uploads",
            files={"file": ("cover.png", io.BytesIO(TINY_PNG), "image/png")},
            headers=auth,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["url"].startswith("/api/v1/uploads/")
        assert data["url"].endswith(".png")
        assert data["size"] == len(TINY_PNG)

    def test_upload_rejects_unsupported_type(self, client, auth):
        resp = client.post(
            "/api/v1/uploads",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            headers=auth,
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["detail"]

    def test_upload_requires_auth(self, client):
        resp = client.post(
            "/api/v1/uploads",
            files={"file": ("img.png", io.BytesIO(TINY_PNG), "image/png")},
        )
        assert resp.status_code == 401

    def test_serve_uploaded_image(self, client, auth):
        upload = client.post(
            "/api/v1/uploads",
            files={"file": ("cover.png", io.BytesIO(TINY_PNG), "image/png")},
            headers=auth,
        ).json()
        resp = client.get(upload["url"])
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert len(resp.content) == len(TINY_PNG)

    def test_serve_nonexistent_returns_404(self, client):
        resp = client.get("/api/v1/uploads/does_not_exist.png")
        assert resp.status_code == 404

    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/v1/uploads/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)


class TestAiGenerateFields:
    def test_requires_auth(self, client):
        resp = client.post(
            "/api/v1/ai/generate-fields",
            json={"title": "Hello", "fields": ["meta_description"]},
        )
        assert resp.status_code == 401

    def test_rejects_empty_input(self, client, auth):
        resp = client.post(
            "/api/v1/ai/generate-fields",
            json={"title": "", "body_markdown": "", "fields": ["meta_description"]},
            headers=auth,
        )
        assert resp.status_code == 400
        assert "title or body_markdown" in resp.json()["detail"]

    def test_rejects_unknown_fields(self, client, auth):
        resp = client.post(
            "/api/v1/ai/generate-fields",
            json={"title": "Hello", "fields": ["not_a_field"]},
            headers=auth,
        )
        assert resp.status_code == 400
        assert "Unknown fields" in resp.json()["detail"]

    def test_generates_when_ai_available(self, client, auth, monkeypatch):
        from app.services import ai

        class FakeCompletion:
            provider = "test"
            model = "test-model"

        monkeypatch.setattr(
            ai,
            "json_completion",
            lambda messages, **kw: (
                {
                    "meta_description": "A great article about testing.",
                    "keywords": ["testing", "pytest", "python"],
                    "tags": ["python", "testing"],
                    "excerpt": "Learn how to test your code.",
                },
                FakeCompletion(),
            ),
        )

        resp = client.post(
            "/api/v1/ai/generate-fields",
            json={
                "title": "Testing in Python",
                "body_markdown": "This is a guide to testing...",
                "fields": ["meta_description", "keywords", "tags", "excerpt"],
            },
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta_description"] == "A great article about testing."
        assert data["keywords"] == ["testing", "pytest", "python"]
        assert data["tags"] == ["python", "testing"]
        assert data["excerpt"] == "Learn how to test your code."
        assert data["provider"] == "test"

    def test_503_when_ai_unavailable(self, client, auth, monkeypatch):
        from app.services import ai

        def _fail(*a, **kw):
            raise ai.AIError("No providers available")

        monkeypatch.setattr(ai, "json_completion", _fail)

        resp = client.post(
            "/api/v1/ai/generate-fields",
            json={"title": "Hello", "fields": ["meta_description"]},
            headers=auth,
        )
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()
