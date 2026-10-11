"""Adversarial input testing across input surfaces.

Crafts inputs designed to break assumptions: path traversal, null bytes,
HTML/script injection in text fields, SQL in strings, oversized payloads,
duplicate keys, mixed encodings, and template injection.
"""
from __future__ import annotations

import pytest

from app.models.project import Project, Tone
from app.models.content import Content, ContentStatus, ContentType


# --------------------------------------------------------------------------- #
# Upload filename traversal                                                    #
# --------------------------------------------------------------------------- #


class TestUploadPathTraversal:
    """The serve endpoint must never escape the upload directory."""

    @pytest.mark.parametrize(
        "filename,expected_status",
        [
            pytest.param("..%5Cetc%5Cpasswd", 400, id="dot-dot-backslash-encoded"),
            pytest.param("file%5Csecret.jpg", 400, id="backslash-encoded"),
            pytest.param("file%00.jpg", 400, id="null-byte-encoded"),
            pytest.param("normal.jpg", 404, id="normal-missing"),
        ],
    )
    def test_traversal_rejected(self, client, filename, expected_status):
        resp = client.get(f"/api/v1/uploads/{filename}")
        assert resp.status_code == expected_status


# --------------------------------------------------------------------------- #
# Content creation with hostile payloads                                       #
# --------------------------------------------------------------------------- #


class TestContentAdversarialInput:
    """Content fields carrying SQL, HTML, or script must be stored as-is."""

    def test_sql_in_title(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "Robert'; DROP TABLE content;--",
                "body_markdown": "Safe body.",
            },
            headers=auth,
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "Robert'; DROP TABLE content;--"

    def test_html_script_in_body(self, client, auth, project):
        body = '<script>alert("xss")</script><p>Hello</p>'
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "XSS Test",
                "body_markdown": body,
            },
            headers=auth,
        )
        assert resp.status_code == 201
        assert resp.json()["body_markdown"] == body

    def test_html_in_title(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": '<img src=x onerror="alert(1)">',
                "body_markdown": "Body.",
            },
            headers=auth,
        )
        assert resp.status_code == 201

    def test_oversized_keywords_rejected(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "Normal",
                "keywords": ["x" * 200],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_null_byte_in_title(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "Null\x00byte",
                "body_markdown": "Body.",
            },
            headers=auth,
        )
        # Should either accept (stored as-is) or reject cleanly — not 500
        assert resp.status_code in (201, 422)

    def test_unicode_bidi_override_in_title(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "Normal ‮evil text",
                "body_markdown": "Body.",
            },
            headers=auth,
        )
        assert resp.status_code in (201, 422)

    def test_extremely_long_tag_list(self, client, auth, project):
        resp = client.post(
            "/api/v1/content",
            json={
                "project_id": project.id,
                "title": "Tag test",
                "tags": ["tag"] * 100,
            },
            headers=auth,
        )
        # max_length=30 on tags field
        assert resp.status_code == 422

    def test_duplicate_platform_in_publish_deduped(self, client, auth, project, db):
        content = Content(
            project_id=project.id,
            content_type=ContentType.FEATURE_SPOTLIGHT,
            status=ContentStatus.APPROVED,
            title="Test",
            slug="test-dedup",
            body_markdown="Body.",
            keywords=[],
            tags=[],
        )
        db.add(content)
        db.commit()
        db.refresh(content)

        resp = client.post(
            f"/api/v1/content/{content.id}/publish",
            json={"platforms": ["devto", "devto"]},
            headers=auth,
        )
        # Should not 500 from unique constraint violation
        assert resp.status_code != 500


# --------------------------------------------------------------------------- #
# Project creation with hostile payloads                                       #
# --------------------------------------------------------------------------- #


class TestProjectAdversarialInput:
    """Project fields should handle hostile input gracefully."""

    def test_sql_in_name(self, client, auth):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "'; DROP TABLE projects;--"},
            headers=auth,
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "'; DROP TABLE projects;--"

    def test_html_in_description(self, client, auth):
        resp = client.post(
            "/api/v1/projects",
            json={
                "name": "HTML test",
                "description": '<script>alert("xss")</script>',
            },
            headers=auth,
        )
        assert resp.status_code == 201

    def test_oversized_tech_stack_entry_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/projects",
            json={
                "name": "Tech test",
                "tech_stack": ["x" * 200],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_whitespace_only_name_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "   \t\n  "},
            headers=auth,
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Webhook URL validation                                                       #
# --------------------------------------------------------------------------- #


class TestWebhookAdversarialInput:
    """Webhook endpoints must reject SSRF-prone URLs."""

    def test_javascript_url_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "javascript:alert(1)",
                "events": ["content.published"],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_data_url_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "data:text/html,<script>alert(1)</script>",
                "events": ["content.published"],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_ftp_url_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "ftp://evil.example.com/payload",
                "events": ["content.published"],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_empty_events_rejected(self, client, auth):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "events": [],
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_oversized_description(self, client, auth):
        resp = client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://example.com/hook",
                "events": ["content.published"],
                "description": "x" * 201,
            },
            headers=auth,
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Tag rename with hostile input                                                #
# --------------------------------------------------------------------------- #


class TestTagAdversarialInput:
    """Tag operations must handle special characters safely."""

    def test_rename_sql_injection(self, client, auth, project, db):
        content = Content(
            project_id=project.id,
            content_type=ContentType.FEATURE_SPOTLIGHT,
            status=ContentStatus.DRAFT,
            title="Tagged",
            slug="tagged",
            body_markdown="Body",
            keywords=[],
            tags=["python"],
        )
        db.add(content)
        db.commit()

        resp = client.post(
            "/api/v1/tags/rename",
            json={
                "old": "python",
                "new": "'; DROP TABLE content;--",
                "dry_run": True,
            },
            headers=auth,
        )
        # Should succeed (the tag is just normalized) or reject — never 500
        assert resp.status_code in (200, 422)

    def test_rename_path_traversal_tag(self, client, auth, project, db):
        content = Content(
            project_id=project.id,
            content_type=ContentType.FEATURE_SPOTLIGHT,
            status=ContentStatus.DRAFT,
            title="Tagged 2",
            slug="tagged-2",
            body_markdown="Body",
            keywords=[],
            tags=["old-tag"],
        )
        db.add(content)
        db.commit()

        resp = client.post(
            "/api/v1/tags/rename",
            json={
                "old": "old-tag",
                "new": "../../../etc/passwd",
                "dry_run": True,
            },
            headers=auth,
        )
        assert resp.status_code in (200, 422)


# --------------------------------------------------------------------------- #
# Auth with hostile input                                                      #
# --------------------------------------------------------------------------- #


class TestAuthAdversarialInput:
    """Auth endpoints must handle hostile payloads without leaking info."""

    def test_sql_in_email(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            data={
                "username": "' OR 1=1;--@example.com",
                "password": "doesntmatter",
            },
        )
        assert resp.status_code in (401, 422)

    def test_oversized_password_rejected(self, client):
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "test@example.com",
                "password": "x" * 200,
            },
        )
        assert resp.status_code == 422

    def test_unicode_password(self, client):
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "unicode@example.com",
                "password": "‮ABCDEFGH",
            },
            headers={"Content-Type": "application/json"},
        )
        # 8 chars visible but one is a control char — should be accepted (len >= 8)
        assert resp.status_code in (201, 409, 422)


# --------------------------------------------------------------------------- #
# Template rendering with hostile values                                       #
# --------------------------------------------------------------------------- #


class TestTemplateAdversarialInput:
    """Template values must not enable injection."""

    def test_template_value_with_placeholder_syntax_not_expanded(self, db, user):
        """A value containing {{...}} must not be re-expanded."""
        from app.models.template import ContentTemplate, TemplateMode
        from app.services import templates

        template = ContentTemplate(
            user_id=user.id,
            name="Inject test",
            mode=TemplateMode.LITERAL,
            content_type=ContentType.ANNOUNCEMENT,
            title_template="{{greeting}}",
            body_template="Hello {{name}}",
            variables=[
                {"name": "greeting", "required": True},
                {"name": "name", "required": True},
            ],
        )

        result = templates.render(
            template,
            values={
                "greeting": "{{name}}",
                "name": "World",
            },
        )
        # The title should be the literal "{{name}}" not "World"
        assert result.title == "{{name}}"
        assert result.body == "Hello World"

    def test_template_value_with_script_tag(self, db, user):
        """A value containing a script tag is stored as-is (no XSS)."""
        from app.models.template import ContentTemplate, TemplateMode
        from app.services import templates

        template = ContentTemplate(
            user_id=user.id,
            name="XSS test",
            mode=TemplateMode.LITERAL,
            content_type=ContentType.ANNOUNCEMENT,
            title_template="Post: {{topic}}",
            body_template="About {{topic}}",
            variables=[{"name": "topic", "required": True}],
        )

        result = templates.render(
            template,
            values={"topic": '<script>alert("xss")</script>'},
        )
        assert '<script>alert("xss")</script>' in result.body


# --------------------------------------------------------------------------- #
# RSS feed with hostile content                                                #
# --------------------------------------------------------------------------- #


class TestRssFeedAdversarialInput:
    """RSS feed generation must escape hostile content."""

    def test_xml_injection_in_title(self):
        from app.services.rss import build_feed

        project = Project(
            name='</title><script>alert("xss")</script>',
            slug="evil",
            description="Normal",
        )
        feed = build_feed(project, [], self_url="https://example.com/feed.xml")
        # The closing </title> must be escaped, not treated as markup
        assert "<script>" not in feed
        assert "&lt;script&gt;" in feed or "alert" not in feed

    def test_cdata_injection_in_description(self):
        from app.services.rss import build_feed

        project = Project(
            name="Normal",
            slug="normal",
            description="]]></description><script>evil</script><description>",
        )
        feed = build_feed(project, [], self_url="https://example.com/feed.xml")
        assert "<script>evil</script>" not in feed

    def test_null_byte_in_title_stripped(self):
        from app.services.rss import build_feed

        project = Project(
            name="Title\x00with\x01control\x08chars",
            slug="ctrl",
            description="Normal",
        )
        feed = build_feed(project, [], self_url="https://example.com/feed.xml")
        assert "\x00" not in feed
        assert "\x01" not in feed
        assert "\x08" not in feed


# --------------------------------------------------------------------------- #
# Formatting sanitization                                                      #
# --------------------------------------------------------------------------- #


class TestFormattingAdversarialInput:
    """HTML sanitization must strip dangerous content."""

    def test_script_tag_stripped(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html('<p>Hello</p><script>alert("xss")</script>')
        assert "<script>" not in result
        assert "Hello" in result

    def test_event_handler_stripped(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html('<img src="x" onerror="alert(1)">')
        assert "onerror" not in result

    def test_javascript_href_stripped(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html('<a href="javascript:alert(1)">Click</a>')
        assert "javascript:" not in result

    def test_data_uri_in_img_stripped(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html(
            '<img src="data:text/html,<script>alert(1)</script>">'
        )
        assert "data:" not in result

    def test_mutation_xss_nested_tags(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html(
            '<noscript><p title="</noscript><img src=x onerror=alert(1)>">test</p></noscript>'
        )
        assert "onerror" not in result

    def test_svg_script_stripped(self):
        from app.services.publishers.formatting import sanitize_html
        result = sanitize_html(
            '<svg onload="alert(1)"><circle cx="50" cy="50" r="40"/></svg>'
        )
        assert "onload" not in result


# --------------------------------------------------------------------------- #
# YAML front matter escaping                                                   #
# --------------------------------------------------------------------------- #


class TestFrontMatterAdversarialInput:
    """YAML front matter must survive hostile scalar values."""

    def test_yaml_injection_via_multiline(self):
        from app.services.publishers.formatting import front_matter
        import yaml

        block = front_matter({
            "title": "Normal\nmalicious_key: true\n---\nbody injection",
        })
        lines = block.splitlines()
        assert lines[0] == "---" and lines[-1] == "---"
        parsed = yaml.safe_load("\n".join(lines[1:-1]))
        assert set(parsed.keys()) == {"title"}
        assert "malicious_key" not in parsed

    def test_yaml_injection_via_bracket(self):
        from app.services.publishers.formatting import front_matter
        import yaml

        block = front_matter({
            "keywords": ['normal", "injected_key: true'],
        })
        parsed = yaml.safe_load("\n".join(block.splitlines()[1:-1]))
        assert parsed["keywords"] == ['normal", "injected_key: true']


# --------------------------------------------------------------------------- #
# Credential field bounds                                                      #
# --------------------------------------------------------------------------- #


class TestCredentialAdversarialInput:
    """Credential payloads must be bounded."""

    def test_oversized_credential_key_rejected(self, client, auth):
        resp = client.put(
            "/api/v1/settings/connections",
            json={
                "platform": "devto",
                "credentials": {"x" * 200: "value"},
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_oversized_credential_value_rejected(self, client, auth):
        resp = client.put(
            "/api/v1/settings/connections",
            json={
                "platform": "devto",
                "credentials": {"api_key": "x" * 3000},
            },
            headers=auth,
        )
        assert resp.status_code == 422

    def test_too_many_credential_fields_rejected(self, client, auth):
        creds = {f"key_{i}": f"val_{i}" for i in range(30)}
        resp = client.put(
            "/api/v1/settings/connections",
            json={
                "platform": "devto",
                "credentials": creds,
            },
            headers=auth,
        )
        assert resp.status_code == 422
