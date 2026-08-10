"""The arm of each adapter that runs when the platform says yes.

The suite is thorough about adapters failing — malformed envelopes, rejected
credentials, private hosts, rate limits — and thin about them succeeding. Four
adapters had never had their ``publish()`` return value read at all: the
``if not <id>: raise`` guard was exercised, the line after it was not.

Also covers the optional halves of the payload builders. Every request fixture
in the suite carries an excerpt, a meta description, a canonical URL and a cover
image, so each ``if request.<field>`` has only been walked one way; a piece
written by hand in the editor has none of them.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.models.publication import Platform
from app.services import link_check
from app.services.publishers import base
from app.services.publishers.base import (
    Adapter,
    NotImplementedAdapter,
    PublishRequest,
    PublishResult,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.buttondown import ButtondownAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.mastodon import MastodonAdapter
from app.services.publishers.medium import MediumAdapter
from app.services.publishers.twitter import TwitterAdapter
from app.services.publishers.wordpress import WordPressAdapter

#: A piece with every optional field filled in.
FULL = PublishRequest(
    title="Automating developer marketing",
    body_markdown="## Why\n\nHerald watches your repos and writes the posts.",
    excerpt="Herald watches your repos.",
    meta_description="Herald automates developer marketing end to end.",
    tags=["python", "automation"],
    canonical_url="https://herald.example.com/blog/automating",
    cover_image_url="https://herald.example.com/cover.png",
    project_url="https://herald.example.com",
    project_name="Herald",
    focus_keyword="developer marketing",
)

#: The same piece as typed into the editor with nothing else filled in.
BARE = PublishRequest(
    title="A short note",
    body_markdown="Just a paragraph.",
    excerpt="",
    meta_description="",
)


class FakeResponse:
    """Enough of an ``httpx.Response`` for the adapters' ``_json`` helper."""

    status_code = 200

    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _answer(adapter: Adapter, monkeypatch, payload: Any) -> list[dict]:
    """Make every request this adapter sends return *payload*. Records the calls."""
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(payload)

    monkeypatch.setattr(adapter, "_request", fake_request)
    return calls


# --------------------------------------------------------------------------- #
# Hashnode                                                                     #
# --------------------------------------------------------------------------- #


def test_hashnode_returns_the_published_post(monkeypatch):
    adapter = HashnodeAdapter()
    _answer(
        adapter,
        monkeypatch,
        {
            "data": {
                "publishPost": {
                    "post": {
                        "id": "post-9",
                        "url": "https://ada.hashnode.dev/automating",
                        "slug": "automating",
                    }
                }
            }
        },
    )

    result = adapter.publish(FULL, {"api_key": "k", "publication_id": "pub-1"})

    assert result == PublishResult(
        external_id="post-9",
        external_url="https://ada.hashnode.dev/automating",
        extra={"slug": "automating"},
    )


def test_hashnode_sends_the_cover_image_and_skips_absent_meta():
    adapter = HashnodeAdapter()

    with_everything = adapter.build_payload(FULL, "pub-1")
    without = adapter.build_payload(BARE, "pub-1")

    assert with_everything["coverImageOptions"] == {
        "coverImageURL": "https://herald.example.com/cover.png"
    }
    assert with_everything["metaTags"]["description"] == FULL.meta_description
    assert "metaTags" not in without
    assert "originalArticleURL" not in without
    assert "coverImageOptions" not in without


def test_hashnode_reads_a_single_error_object_that_is_not_in_a_list(monkeypatch):
    """GraphQL says ``errors`` is a list. A gateway in the way may disagree."""
    adapter = HashnodeAdapter()
    _answer(adapter, monkeypatch, {"errors": {"message": "publication not found"}})

    with pytest.raises(base.PublishError, match="publication not found"):
        adapter.publish(FULL, {"api_key": "k", "publication_id": "pub-1"})


# --------------------------------------------------------------------------- #
# Bluesky                                                                      #
# --------------------------------------------------------------------------- #


_SESSION = {"accessJwt": "jwt", "did": "did:plc:abc", "handle": "ada.bsky.social"}


def test_bluesky_verify_names_the_handle_the_session_came_back_with(monkeypatch):
    adapter = BlueskyAdapter()
    _answer(adapter, monkeypatch, _SESSION)

    assert adapter.verify({"handle": "@ada", "app_password": "p"}) == "@ada.bsky.social"


def test_bluesky_returns_the_at_uri_and_attaches_link_facets(monkeypatch):
    adapter = BlueskyAdapter()
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("createSession"):
            return FakeResponse(_SESSION)
        return FakeResponse(
            {"uri": "at://did:plc:abc/app.bsky.feed.post/3kabc", "cid": "bafy"}
        )

    monkeypatch.setattr(adapter, "_request", fake_request)

    result = adapter.publish(FULL, {"handle": "@ada", "app_password": "p"})

    assert result.external_id == "at://did:plc:abc/app.bsky.feed.post/3kabc"
    assert "3kabc" in result.external_url
    assert "ada.bsky.social" in result.external_url
    # The link is in the text, so it must be in the facets — Bluesky does not
    # linkify anything the record does not annotate.
    record = calls[-1]["json_body"]["record"]
    assert record["facets"], "a post carrying a link must carry its facets"


def test_bluesky_omits_facets_when_the_post_carries_no_link(monkeypatch):
    adapter = BlueskyAdapter()
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("createSession"):
            return FakeResponse(_SESSION)
        return FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz"})

    monkeypatch.setattr(adapter, "_request", fake_request)

    adapter.publish(BARE, {"handle": "@ada", "app_password": "p"})

    assert "facets" not in calls[-1]["json_body"]["record"]


# --------------------------------------------------------------------------- #
# WordPress                                                                    #
# --------------------------------------------------------------------------- #


_WP_CREDENTIALS = {
    "site_url": "https://blog.example.com",
    "username": "ada",
    "application_password": "abcd efgh",
}


def test_wordpress_returns_the_permalink(monkeypatch):
    adapter = WordPressAdapter()
    _answer(
        adapter,
        monkeypatch,
        {
            "id": 41,
            "link": "https://blog.example.com/automating",
            "status": "publish",
            "slug": "automating",
        },
    )

    result = adapter.publish(FULL, _WP_CREDENTIALS)

    assert result.external_id == "41"
    assert result.external_url == "https://blog.example.com/automating"
    assert result.extra == {"status": "publish", "slug": "automating"}


def test_wordpress_falls_back_to_the_editor_url_for_a_draft(monkeypatch):
    adapter = WordPressAdapter()
    _answer(adapter, monkeypatch, {"id": 42, "status": "draft", "slug": "note"})

    result = adapter.publish(BARE, _WP_CREDENTIALS)

    assert result.external_url == (
        "https://blog.example.com/wp-admin/post.php?post=42&action=edit"
    )


def test_wordpress_sends_no_yoast_meta_when_there_is_none_to_send():
    adapter = WordPressAdapter()

    full = adapter.build_payload(FULL)
    bare = adapter.build_payload(BARE)

    assert full["excerpt"] == FULL.excerpt
    assert full["meta"]["_yoast_wpseo_metadesc"] == FULL.meta_description
    assert full["meta"]["_yoast_wpseo_focuskw"] == "developer marketing"
    assert full["meta"]["_yoast_wpseo_canonical"] == FULL.canonical_url
    assert "excerpt" not in bare
    assert "meta" not in bare


# --------------------------------------------------------------------------- #
# Mastodon                                                                     #
# --------------------------------------------------------------------------- #


_MASTO_CREDENTIALS = {
    "instance_url": "https://fosstodon.org",
    "access_token": "t",
}


def test_mastodon_returns_the_status_url(monkeypatch):
    adapter = MastodonAdapter()
    _answer(
        adapter,
        monkeypatch,
        {
            "id": "109",
            "url": "https://fosstodon.org/@ada/109",
            "visibility": "public",
        },
    )

    result = adapter.publish(FULL, _MASTO_CREDENTIALS)

    assert result.external_id == "109"
    assert result.external_url == "https://fosstodon.org/@ada/109"
    assert result.extra == {"visibility": "public"}


def test_mastodon_sends_the_idempotency_key_when_the_request_carries_one(monkeypatch):
    adapter = MastodonAdapter()
    calls = _answer(adapter, monkeypatch, {"id": "110", "uri": "https://x/110"})

    keyed = PublishRequest(
        title=BARE.title,
        body_markdown=BARE.body_markdown,
        excerpt="",
        meta_description="",
        idempotency_key="content-7-mastodon",
    )
    adapter.publish(keyed, _MASTO_CREDENTIALS)

    assert calls[-1]["headers"]["Idempotency-Key"] == "content-7-mastodon"


def test_mastodon_refuses_an_instance_url_with_no_host():
    adapter = MastodonAdapter()

    with pytest.raises(base.CredentialError, match="not a usable instance URL"):
        adapter._api("https:///nowhere")


# --------------------------------------------------------------------------- #
# The rest of the payload builders                                             #
# --------------------------------------------------------------------------- #


def test_medium_omits_the_canonical_when_the_piece_is_the_original(monkeypatch):
    adapter = MediumAdapter()
    calls = _answer(adapter, monkeypatch, {"data": {"id": "p1", "url": "https://m/p1"}})

    adapter.publish(BARE, {"integration_token": "t", "publication_id": "pub"})

    assert "canonicalUrl" not in calls[-1]["json_body"]


def test_devto_sends_the_cover_image_when_there_is_one(monkeypatch):
    adapter = DevToAdapter()
    calls = _answer(adapter, monkeypatch, {"id": 5, "url": "https://dev.to/ada/x"})

    adapter.publish(FULL, {"api_key": "k"})

    article = calls[-1]["json_body"]["article"]
    assert article["main_image"] == "https://herald.example.com/cover.png"


def test_a_twitter_thread_with_no_tags_gets_no_hashtag_tweet():
    adapter = TwitterAdapter()

    assert adapter.build_thread(BARE)[-1] != ""
    assert not any(post.startswith("#") for post in adapter.build_thread(BARE))
    assert adapter.build_thread(FULL)[-1].startswith("#")


def test_a_linkedin_post_with_no_tags_is_just_the_lead():
    adapter = LinkedInAdapter()

    assert "#" not in adapter.build_commentary(BARE)
    assert "#python" in adapter.build_commentary(FULL)

    payload = adapter.build_payload(FULL, "urn:li:person:abc")
    assert payload["author"] == "urn:li:person:abc"
    assert payload["lifecycleState"] == "PUBLISHED"


def test_buttondown_refuses_a_newsletter_that_is_not_an_object(monkeypatch):
    adapter = ButtondownAdapter()
    _answer(adapter, monkeypatch, {"results": ["not-an-object"]})

    with pytest.raises(base.CredentialError, match="unexpected newsletter"):
        adapter.verify({"api_key": "k"})


# --------------------------------------------------------------------------- #
# Shared adapter machinery                                                     #
# --------------------------------------------------------------------------- #


class _Nameless(Adapter):
    """An adapter that has not been taught to verify anything."""

    platform = Platform.MEDIUM
    display_name = "Nameless"

    def publish(self, request, credentials):  # pragma: no cover - never called
        raise AssertionError("not used")


def test_an_adapter_that_cannot_verify_says_so_rather_than_pretending():
    with pytest.raises(NotImplementedAdapter, match="cannot verify credentials yet"):
        _Nameless().verify({})


def test_the_default_metrics_snapshot_is_empty_rather_than_an_error():
    snapshot = _Nameless().fetch_metrics("1", {})

    assert snapshot.views is None
    assert snapshot.reads is None
    assert snapshot.reactions is None


def test_a_redirect_with_nowhere_to_go_is_handed_back_rather_than_guessed_at(
    monkeypatch,
):
    """A 3xx with no ``Location``. Inventing a destination would be worse."""
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)
    monkeypatch.setattr(
        base.httpx,
        "request",
        lambda method, url, **kwargs: httpx.Response(
            302, request=httpx.Request(method, url)
        ),
    )

    resp = WordPressAdapter()._request("GET", "https://blog.example.com/wp-json")

    assert resp.status_code == 302


def test_a_retry_after_date_with_no_timezone_is_read_as_utc():
    resp = httpx.Response(
        429,
        headers={"Retry-After": "Sun, 06 Nov 2094 08:49:37 -0000"},
        request=httpx.Request("GET", "https://x.test/"),
    )

    assert base._retry_after(resp) > 0


def test_an_unparseable_retry_after_date_reads_as_no_guidance(monkeypatch):
    monkeypatch.setattr(base.email.utils, "parsedate_to_datetime", lambda raw: None)
    resp = httpx.Response(
        429,
        headers={"Retry-After": "next Tuesday"},
        request=httpx.Request("GET", "https://x.test/"),
    )

    assert base._retry_after(resp) is None


def test_the_sleep_indirection_actually_sleeps():
    """Pins the one line the whole retry suite replaces to do its work."""
    base._sleep(0)
