"""Medium adapter — the paths ``test_canonical.py`` and ``test_cover_image.py``
never touch: they only ever exercise ``publish`` with a ``publication_id``
already in hand. ``verify``, the profile-post branch that has to look its own
user id up first, the tag cap, and title escaping are covered here.
"""
from __future__ import annotations

import pytest

from app.services.publishers.base import CredentialError, PublishError, PublishRequest
from app.services.publishers.medium import MediumAdapter


@pytest.fixture
def adapter() -> MediumAdapter:
    return MediumAdapter()


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing & <ops>",
        body_markdown="## Why\n\nPulse watches your repos and writes the posts.",
        excerpt="Pulse watches your repos.",
        meta_description="Pulse automates developer marketing end to end.",
        tags=["python", "dev-tools", "AI", "automation", "extra", "sixth"],
        canonical_url="https://pulse.example.com/blog/automating",
        project_url="https://pulse.example.com",
        project_name="Pulse",
    )


class FakeResponse:
    """Just enough httpx.Response for an adapter."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# verify()                                                                     #
# --------------------------------------------------------------------------- #


def test_verify_returns_the_username(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse({"data": {"id": "u1", "username": "ada"}}),
    )

    assert adapter.verify({"integration_token": "t"}) == "@ada"


def test_verify_falls_back_to_the_display_name_with_no_username(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse({"data": {"id": "u1", "name": "Ada Lovelace"}}),
    )

    assert adapter.verify({"integration_token": "t"}) == "Ada Lovelace"


def test_verify_with_no_user_id_is_a_credential_error(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_request", lambda *a, **kw: FakeResponse({"data": {}}))

    with pytest.raises(CredentialError):
        adapter.verify({"integration_token": "t"})


def test_a_missing_token_is_a_credential_error(adapter, request_):
    with pytest.raises(CredentialError) as exc:
        adapter.publish(request_, {})

    assert "integration_token" in str(exc.value)


# --------------------------------------------------------------------------- #
# publish() — with and without a publication                                  #
# --------------------------------------------------------------------------- #


def test_publish_with_a_publication_id_posts_to_the_publication(
    adapter, request_, monkeypatch
):
    sent: dict = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda method, url, **kw: (sent.update(url=url, **kw), FakeResponse(
            {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}
        ))[1],
    )

    result = adapter.publish(request_, {"integration_token": "t", "publication_id": "pub-1"})

    assert sent["url"] == "https://api.medium.com/v1/publications/pub-1/posts"
    assert result.external_id == "p1"
    assert result.external_url == "https://medium.com/@x/y"


def test_publish_with_no_publication_id_looks_up_the_user_and_posts_to_their_profile(
    adapter, request_, monkeypatch
):
    """The un-exercised branch: no ``publication_id`` means a ``/me`` lookup
    before the post, and the post goes to that user's own feed."""
    calls: list[str] = []

    def fake_request(method, url, **kw):
        calls.append(url)
        if url.endswith("/me"):
            return FakeResponse({"data": {"id": "u1", "username": "ada"}})
        return FakeResponse({"data": {"id": "p1", "url": "https://medium.com/@ada/y"}})

    monkeypatch.setattr(adapter, "_request", fake_request)

    result = adapter.publish(request_, {"integration_token": "t"})

    assert calls == [
        "https://api.medium.com/v1/me",
        "https://api.medium.com/v1/users/u1/posts",
    ]
    assert result.external_id == "p1"


def test_a_response_with_no_post_id_is_a_publish_error(adapter, request_, monkeypatch):
    monkeypatch.setattr(adapter, "_request", lambda *a, **kw: FakeResponse({"data": {}}))

    with pytest.raises(PublishError):
        adapter.publish(request_, {"integration_token": "t", "publication_id": "pub-1"})


# --------------------------------------------------------------------------- #
# Payload shape                                                                #
# --------------------------------------------------------------------------- #


def test_payload_caps_tags_at_five(adapter, request_, monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: (sent.update(kw), FakeResponse(
            {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}
        ))[1],
    )

    adapter.publish(request_, {"integration_token": "t", "publication_id": "pub-1"})

    assert len(sent["json_body"]["tags"]) == 5


def test_the_title_is_html_escaped_in_the_body(adapter, request_, monkeypatch):
    """The title is injected as a literal ``<h1>`` — anything it carries from a
    model's output has to be escaped or it becomes markup instead of text."""
    sent: dict = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: (sent.update(kw), FakeResponse(
            {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}
        ))[1],
    )

    adapter.publish(request_, {"integration_token": "t", "publication_id": "pub-1"})

    content = sent["json_body"]["content"]
    assert "<h1>Automating developer marketing &amp; &lt;ops&gt;</h1>" in content


def test_canonical_url_is_forwarded_when_present(adapter, request_, monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: (sent.update(kw), FakeResponse(
            {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}
        ))[1],
    )

    adapter.publish(request_, {"integration_token": "t", "publication_id": "pub-1"})

    assert sent["json_body"]["canonicalUrl"] == request_.canonical_url


def test_as_draft_maps_to_a_draft_publish_status(adapter, request_, monkeypatch):
    from dataclasses import replace

    sent: dict = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: (sent.update(kw), FakeResponse(
            {"data": {"id": "p1", "url": "https://medium.com/@x/y"}}
        ))[1],
    )

    adapter.publish(
        replace(request_, as_draft=True),
        {"integration_token": "t", "publication_id": "pub-1"},
    )

    assert sent["json_body"]["publishStatus"] == "draft"
