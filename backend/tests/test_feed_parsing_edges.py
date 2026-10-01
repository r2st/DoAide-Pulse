"""Feed parsing on the malformed input real feeds actually ship.

Dates that are neither RFC 822 nor ISO 8601, Atom entries whose only usable
link is not the ``alternate`` one, and link elements with an empty href. None of
these should cost the whole feed — an entry with an unparseable date is still an
entry, and a trigger that refuses the feed stops firing entirely.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import feeds

ATOM = "http://www.w3.org/2005/Atom"


def _atom(entry_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="{ATOM}">
  <title>Example</title>
  {entry_xml}
</feed>""".encode()


# --------------------------------------------------------------------------- #
# Dates                                                                       #
# --------------------------------------------------------------------------- #


def test_an_rfc_822_date_parses():
    assert feeds._parse_date("Mon, 20 Jul 2026 09:00:00 +0000") == datetime(
        2026, 7, 20, 9, 0, tzinfo=UTC
    )


def test_an_iso_date_with_a_z_parses():
    assert feeds._parse_date("2026-07-20T09:00:00Z") == datetime(
        2026, 7, 20, 9, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("raw", ["", "   ", "yesterday", "20/07/2026", "not a date"])
def test_a_date_in_neither_format_is_simply_absent(raw):
    """No date is a fact the entry can carry. A crash is not."""
    assert feeds._parse_date(raw) is None


# --------------------------------------------------------------------------- #
# Atom links                                                                  #
# --------------------------------------------------------------------------- #


def test_an_empty_href_is_skipped_in_favour_of_a_real_one():
    body = _atom(
        """<entry>
    <title>Post</title>
    <id>urn:1</id>
    <link rel="alternate" href="" />
    <link rel="alternate" href="https://example.com/post" />
  </entry>"""
    )

    feed = feeds.parse(body)

    assert feed.entries[0].link == "https://example.com/post"


def test_a_non_alternate_link_is_used_when_that_is_all_there_is():
    """Plenty of feeds only ship rel="via" or rel="related" on an entry."""
    body = _atom(
        """<entry>
    <title>Post</title>
    <id>urn:2</id>
    <link rel="related" href="https://example.com/related" />
  </entry>"""
    )

    feed = feeds.parse(body)

    assert feed.entries[0].link == "https://example.com/related"


def test_an_alternate_link_wins_over_an_earlier_one_of_another_kind():
    body = _atom(
        """<entry>
    <title>Post</title>
    <id>urn:3</id>
    <link rel="related" href="https://example.com/related" />
    <link rel="alternate" href="https://example.com/post" />
  </entry>"""
    )

    feed = feeds.parse(body)

    assert feed.entries[0].link == "https://example.com/post"


def test_an_entry_with_no_usable_link_still_parses():
    body = _atom(
        """<entry>
    <title>Post</title>
    <id>urn:4</id>
    <link rel="alternate" href="  " />
  </entry>"""
    )

    feed = feeds.parse(body)

    assert feed.entries[0].link == ""
    assert feed.entries[0].title == "Post"


# --------------------------------------------------------------------------- #
# The fetch client                                                            #
# --------------------------------------------------------------------------- #


def test_the_feed_client_never_follows_a_redirect():
    """A 3xx can move a validated public URL to somewhere inside the network."""
    with feeds._client() as client:
        assert client.follow_redirects is False
        assert "Pulse" in client.headers["User-Agent"]


def test_a_transport_failure_is_reported_as_a_feed_error_not_an_httpx_one():
    import httpx

    class _Dead(httpx.Client):
        def stream(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    with pytest.raises(feeds.FeedError, match="Could not reach that feed"):
        feeds.fetch("https://example.com/feed.xml", client=_Dead())
