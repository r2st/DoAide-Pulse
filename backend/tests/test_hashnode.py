"""Hashnode adapter — the GraphQL error envelope is well covered by
``test_adapter_malformed_response.py``, but the happy paths never run: a
successful ``verify()`` (with and without publications to list) and the
``createDraft`` branch of ``publish()`` are exercised nowhere.
"""
from __future__ import annotations

import pytest

from app.services.publishers.base import PublishError, PublishRequest
from app.services.publishers.hashnode import HashnodeAdapter


@pytest.fixture
def adapter() -> HashnodeAdapter:
    return HashnodeAdapter()


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        body_markdown="## Why\n\nHerald watches your repos and writes the posts.",
        excerpt="Herald watches your repos.",
        meta_description="Herald automates developer marketing end to end.",
        tags=["python", "automation"],
        canonical_url="https://herald.example.com/blog/automating",
        project_url="https://herald.example.com",
        project_name="Herald",
    )


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# verify()                                                                     #
# --------------------------------------------------------------------------- #


def test_verify_lists_the_publications_the_token_can_see(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse(
            {
                "data": {
                    "me": {
                        "username": "ada",
                        "publications": {
                            "edges": [
                                {"node": {"id": "pub-1", "title": "Ada's Blog"}},
                            ]
                        },
                    }
                }
            }
        ),
    )

    result = adapter.verify({"api_key": "k"})

    assert result == "@ada — publications: Ada's Blog (pub-1)"


def test_verify_with_no_publications_still_returns_the_username(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse(
            {"data": {"me": {"username": "ada", "publications": {"edges": []}}}}
        ),
    )

    assert adapter.verify({"api_key": "k"}) == "@ada"


# --------------------------------------------------------------------------- #
# publish() — the draft branch                                                #
# --------------------------------------------------------------------------- #


def test_as_draft_uses_the_create_draft_mutation(adapter, request_, monkeypatch):
    from dataclasses import replace

    sent: dict = {}

    def fake_request(method, url, **kw):
        sent.update(kw)
        return FakeResponse(
            {"data": {"createDraft": {"draft": {"id": "d1", "slug": "automating"}}}}
        )

    monkeypatch.setattr(adapter, "_request", fake_request)

    result = adapter.publish(
        replace(request_, as_draft=True), {"api_key": "k", "publication_id": "pub-1"}
    )

    assert "CreateDraft" in sent["json_body"]["query"]
    assert result.external_id == "d1"
    assert result.external_url == "https://hashnode.com/draft/d1"
    assert result.extra == {"slug": "automating", "draft": True}


def test_a_draft_response_with_no_id_is_a_publish_error(adapter, request_, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse({"data": {"createDraft": {"draft": {}}}}),
    )

    with pytest.raises(PublishError):
        adapter.publish(
            replace(request_, as_draft=True), {"api_key": "k", "publication_id": "pub-1"}
        )
