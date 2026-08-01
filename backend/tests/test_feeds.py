"""RSS and Atom parsing, and the guards around fetching a stranger's URL."""
from __future__ import annotations

import httpx
import pytest

from app.services import feeds

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Herald Changelog</title>
    <link>https://example.com</link>
    <item>
      <title>Shipped webhook triggers</title>
      <link>https://example.com/webhook-triggers</link>
      <guid isPermaLink="false">post-2</guid>
      <description>Teaser.</description>
      <content:encoded>The full post body.</content:encoded>
      <pubDate>Tue, 29 Jul 2026 09:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Shipped RSS triggers</title>
      <link>https://example.com/rss-triggers</link>
      <guid isPermaLink="false">post-1</guid>
      <description>Older news.</description>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Status</title>
  <entry>
    <title>All systems operational</title>
    <id>urn:uuid:1234</id>
    <link rel="edit" href="https://example.com/edit/1"/>
    <link rel="alternate" href="https://status.example.com/1"/>
    <updated>2026-07-30T12:00:00Z</updated>
    <summary>Nothing to report.</summary>
  </entry>
</feed>
"""


def test_rss_is_parsed_with_content_encoded_preferred_over_description():
    feed = feeds.parse(RSS)

    assert feed.title == "Herald Changelog"
    assert [e.entry_id for e in feed.entries] == ["post-2", "post-1"]
    assert feed.entries[0].summary == "The full post body."
    assert feed.entries[0].published_at is not None


def test_atom_is_parsed_and_prefers_the_alternate_link():
    feed = feeds.parse(ATOM)

    assert feed.title == "Status"
    entry = feed.entries[0]
    assert entry.entry_id == "urn:uuid:1234"
    assert entry.link == "https://status.example.com/1"
    assert entry.summary == "Nothing to report."


def test_an_entry_with_no_guid_gets_a_stable_hashed_id():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
      <item><title>Same</title><link>https://example.com/a</link></item>
    </channel></rss>"""

    first = feeds.parse(xml).entries[0].entry_id
    second = feeds.parse(xml).entries[0].entry_id

    assert first == second
    assert first.startswith("h:")


def test_a_document_that_is_not_a_feed_is_refused():
    with pytest.raises(feeds.FeedError, match="is the URL a feed"):
        feeds.parse("<html><body>Not a feed</body></html>")

    with pytest.raises(feeds.FeedError, match="not valid XML"):
        feeds.parse("{\"json\": true}")


def test_a_private_address_is_refused_before_any_request_is_made():
    with pytest.raises(feeds.FeedError):
        feeds.validate_feed_url("http://127.0.0.1:8000/feed.xml")
    with pytest.raises(feeds.FeedError, match="http:// or https://"):
        feeds.validate_feed_url("ftp://example.com/feed.xml")


def test_the_first_poll_of_a_feed_writes_about_nothing():
    """A five-year-old blog must not produce five years of posts."""
    feed = feeds.parse(RSS)

    assert feeds.new_entries(feed, None) == []
    # ...but every id is remembered, so the next poll starts from here.
    assert set(feeds.remember(None, feed)) == {"post-1", "post-2"}


def test_only_unseen_entries_come_back_as_new():
    feed = feeds.parse(RSS)

    fresh = feeds.new_entries(feed, ["post-1"])

    assert [e.entry_id for e in fresh] == ["post-2"]


def test_the_watermark_is_bounded_and_newest_first():
    feed = feeds.parse(RSS)
    old = [f"old-{i}" for i in range(feeds.SEEN_IDS_KEPT)]

    remembered = feeds.remember(old, feed)

    assert remembered[:2] == ["post-2", "post-1"]
    assert len(remembered) == feeds.SEEN_IDS_KEPT


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_parses_a_live_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/feed.xml"
        return httpx.Response(200, content=RSS.encode())

    feed = feeds.fetch("https://example.com/feed.xml", client=_client(handler))

    assert len(feed.entries) == 2


def test_fetch_refuses_a_redirect_rather_than_following_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    with pytest.raises(feeds.FeedError, match="does not follow"):
        feeds.fetch("https://example.com/feed.xml", client=_client(handler))


def test_fetch_abandons_a_feed_that_exceeds_the_size_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss>" + b"x" * (feeds.MAX_FEED_BYTES + 10))

    with pytest.raises(feeds.FeedError, match="larger than"):
        feeds.fetch("https://example.com/feed.xml", client=_client(handler))


def test_fetch_reports_an_http_error_rather_than_raising_httpx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(feeds.FeedError, match="404"):
        feeds.fetch("https://example.com/feed.xml", client=_client(handler))
