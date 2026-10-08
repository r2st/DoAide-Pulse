"""Cover images, from the content row through to each adapter's payload.

The platforms disagree about how to accept one, which is the whole reason this
needs testing: two take a URL in a named field, and two have no such field and
take the first image in the body instead.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentType
from app.services import publishing_service, seo
from app.services.publishers import formatting
from app.services.publishers.base import PublishRequest
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.medium import MediumAdapter
from app.services.publishers.wordpress import WordPressAdapter

COVER = "https://cdn.example.com/cover.png"


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        body_markdown="## Why\n\nPulse writes the posts.\n",
        excerpt="Pulse writes the posts.",
        meta_description="Pulse automates developer marketing.",
        tags=["python"],
        cover_image_url=COVER,
    )


@pytest.fixture
def no_cover(request_) -> PublishRequest:
    from dataclasses import replace

    return replace(request_, cover_image_url=None)


# --------------------------------------------------------------------------- #
# Platforms with a cover field                                                 #
# --------------------------------------------------------------------------- #


def test_devto_sends_main_image(request_, no_cover, monkeypatch):
    sent: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": 1, "url": "https://dev.to/x/y"}

    adapter = DevToAdapter()
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: (sent.update(kw), FakeResponse())[1]
    )

    adapter.publish(request_, {"api_key": "k"})
    assert sent["json_body"]["article"]["main_image"] == COVER

    # Same rule as canonical_url: Forem 422s on an empty string, so with no
    # cover the key must be absent rather than blank.
    adapter.publish(no_cover, {"api_key": "k"})
    assert "main_image" not in sent["json_body"]["article"]


def test_hashnode_sends_cover_image_options(request_, no_cover):
    payload = HashnodeAdapter().build_payload(request_, "pub-123")
    assert payload["coverImageOptions"] == {"coverImageURL": COVER}

    assert "coverImageOptions" not in HashnodeAdapter().build_payload(no_cover, "pub-123")


# --------------------------------------------------------------------------- #
# Platforms without one                                                        #
# --------------------------------------------------------------------------- #


def test_medium_puts_the_cover_above_the_title(request_, no_cover, monkeypatch):
    """Medium has no cover parameter — it uses the first image in the body."""
    sent: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}

    adapter = MediumAdapter()
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: (sent.update(kw), FakeResponse())[1]
    )

    adapter.publish(request_, {"integration_token": "t", "publication_id": "pub"})
    content = sent["json_body"]["content"]
    assert content.startswith(f'<figure><img src="{COVER}"')
    # Above the H1: the *first* image is the one Medium takes as the preview.
    assert content.index("<figure>") < content.index("<h1>")

    adapter.publish(no_cover, {"integration_token": "t", "publication_id": "pub"})
    assert "<figure>" not in sent["json_body"]["content"]
    assert sent["json_body"]["content"].startswith("<h1>")


def test_wordpress_inlines_the_cover(request_, no_cover):
    payload = WordPressAdapter().build_payload(request_)
    assert payload["content"].startswith(f'<figure><img src="{COVER}"')

    assert "<figure>" not in WordPressAdapter().build_payload(no_cover)["content"]


def test_the_lead_image_escapes_its_attributes():
    """A URL with a quote in it must not be able to close the attribute."""
    html = formatting.lead_image_html(
        'https://x.example/a"onerror="alert(1)', alt='He said "hi" & left'
    )
    assert '"onerror="' not in html
    assert "&quot;" in html
    assert "&amp;" in html


def test_the_lead_image_of_nothing_is_nothing():
    assert formatting.lead_image_html("") == ""


# --------------------------------------------------------------------------- #
# The row, the request, the audit                                              #
# --------------------------------------------------------------------------- #


def test_build_request_carries_the_cover(db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="Covered",
        slug="covered",
        cover_image_url=COVER,
    )
    db.add(content)
    db.commit()

    assert publishing_service.build_request(content).cover_image_url == COVER


def test_the_audit_asks_for_a_cover_image():
    issues = seo.audit(
        title="A perfectly fine title about pulse",
        body_markdown="## Section\n\n" + ("word " * 400),
        meta_description="A meta description of an entirely reasonable length for this post.",
        keywords=["pulse"],
    )
    assert any(i.field == "cover_image_url" for i in issues)

    with_cover = seo.audit(
        title="A perfectly fine title about pulse",
        body_markdown="## Section\n\n" + ("word " * 400),
        meta_description="A meta description of an entirely reasonable length for this post.",
        keywords=["pulse"],
        cover_image_url=COVER,
    )
    assert not any(i.field == "cover_image_url" for i in with_cover)


# --------------------------------------------------------------------------- #
# The API                                                                      #
# --------------------------------------------------------------------------- #


def test_a_cover_can_be_set_and_read_back(client, auth, project):
    created = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Covered",
            "body_markdown": "## Body\n\nWords.",
            "cover_image_url": COVER,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["cover_image_url"] == COVER

    content_id = created.json()["id"]
    patched = client.patch(
        f"/api/v1/content/{content_id}",
        headers=auth,
        json={"cover_image_url": "https://cdn.example.com/other.png"},
    )
    assert patched.json()["cover_image_url"] == "https://cdn.example.com/other.png"


def test_a_relative_cover_url_is_refused(client, auth, project):
    """The platforms fetch this from their own servers; a path means nothing."""
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Covered",
            "cover_image_url": "/static/cover.png",
        },
    )
    assert resp.status_code == 422
    assert "absolute" in resp.text


def test_a_blank_cover_url_is_stored_as_nothing(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={"project_id": project.id, "title": "Bare", "cover_image_url": "  "},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["cover_image_url"] is None


def test_the_link_check_covers_the_cover_image(client, auth, project, db, monkeypatch):
    """A dead cover is a visibly broken card in every feed, not a hidden 404."""
    from app.services import link_check

    content = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="Covered",
        slug="covered",
        body_markdown="No links here.",
        cover_image_url=COVER,
    )
    db.add(content)
    db.commit()

    checked: list[list[str] | None] = []
    monkeypatch.setattr(
        link_check,
        "check_body",
        lambda body, extra_urls=None, **kw: checked.append(extra_urls) or [],
    )

    resp = client.get(f"/api/v1/content/{content.id}/links", headers=auth)
    assert resp.status_code == 200, resp.text
    assert checked == [[COVER]]
