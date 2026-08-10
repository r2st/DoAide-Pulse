"""``fetch_metrics`` on Dev.to, Mastodon and Bluesky.

Every test that exercises the metrics-polling task (``test_metrics_rate_limit.py``,
``test_analytics_rates.py``) monkeypatches ``fetch_metrics`` wholesale to avoid
touching the network — which means the parsing inside each adapter's own
``fetch_metrics`` (which JSON field maps to which ``MetricsSnapshot`` field, and
what happens when Bluesky reports a deleted post) has never actually run.
"""
from __future__ import annotations

from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.mastodon import MastodonAdapter


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# Dev.to                                                                       #
# --------------------------------------------------------------------------- #


def test_devto_metrics_map_forem_fields_to_the_snapshot(monkeypatch):
    adapter = DevToAdapter()
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse(
            {
                "page_views_count": 512,
                "public_reactions_count": 9,
                "comments_count": 3,
            }
        ),
    )

    snapshot = adapter.fetch_metrics("123", {"api_key": "k"})

    assert snapshot.views == 512
    assert snapshot.reactions == 9
    assert snapshot.comments == 3
    assert snapshot.shares is None


# --------------------------------------------------------------------------- #
# Mastodon                                                                     #
# --------------------------------------------------------------------------- #


def test_mastodon_metrics_map_status_fields_to_the_snapshot(monkeypatch):
    adapter = MastodonAdapter()
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse(
            {"favourites_count": 4, "replies_count": 2, "reblogs_count": 7}
        ),
    )

    snapshot = adapter.fetch_metrics(
        "1", {"instance_url": "https://fosstodon.org", "access_token": "t"}
    )

    assert snapshot.reactions == 4
    assert snapshot.comments == 2
    assert snapshot.shares == 7
    # Mastodon does not report views at all — None, not zero.
    assert snapshot.views is None


# --------------------------------------------------------------------------- #
# Bluesky                                                                      #
# --------------------------------------------------------------------------- #


_SESSION_REPLY = {"accessJwt": "jwt", "did": "did:plc:abc", "handle": "me.bsky.social"}


def _fake_bluesky_request(get_posts_reply):
    def fake(method, url, **kw):
        if url.endswith("/xrpc/com.atproto.server.createSession"):
            return FakeResponse(_SESSION_REPLY)
        return FakeResponse(get_posts_reply)

    return fake


def test_bluesky_metrics_map_post_fields_to_the_snapshot(monkeypatch):
    adapter = BlueskyAdapter()
    monkeypatch.setattr(
        adapter,
        "_request",
        _fake_bluesky_request(
            {"posts": [{"likeCount": 5, "replyCount": 1, "repostCount": 2}]}
        ),
    )

    snapshot = adapter.fetch_metrics(
        "at://did:plc:abc/app.bsky.feed.post/xyz",
        {"handle": "me.bsky.social", "app_password": "pw"},
    )

    assert snapshot.reactions == 5
    assert snapshot.comments == 1
    assert snapshot.shares == 2
    # Bluesky reports no view count at all.
    assert snapshot.views is None


def test_bluesky_metrics_for_a_deleted_post_is_an_empty_snapshot(monkeypatch):
    """No posts back means gone, or access lost — not an error worth retrying."""
    adapter = BlueskyAdapter()
    monkeypatch.setattr(adapter, "_request", _fake_bluesky_request({"posts": []}))

    snapshot = adapter.fetch_metrics(
        "at://did:plc:abc/app.bsky.feed.post/xyz",
        {"handle": "me.bsky.social", "app_password": "pw"},
    )

    assert snapshot.reactions is None
    assert snapshot.comments is None
    assert snapshot.shares is None
