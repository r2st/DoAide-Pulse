"""RSS 2.0 feed generation for a project's published content.

Public and unauthenticated by design — an RSS reader has no bearer token to
send, and everything in the feed is already live on whatever platform it was
published to. Only ``ContentStatus.PUBLISHED`` rows are ever included: a draft
or review-queue item leaking into a public feed would be a bigger problem than
any RSS feature is worth.
"""
from __future__ import annotations

import re
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

from app.models.content import Content
from app.models.project import Project

#: Characters XML 1.0 has no representation for — not even as a numeric
#: reference. A title carrying one makes the whole feed unparseable for every
#: subscriber, not just that item, and titles here are model output and free
#: text from the editor. Stripped rather than escaped because there is nothing
#: to escape them *to*.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]")


def _clean(value: str) -> str:
    """Escape *value* for XML character data, minus what XML cannot carry."""
    return escape(_ILLEGAL.sub("", value or ""))

#: Enough for a reader to see real history without the response growing
#: unbounded as a project accumulates years of posts.
FEED_ITEM_LIMIT = 50


def _item_link(content: Content, site_url: str) -> str:
    """Where this item points: its own canonical URL, or a slug under the
    project's site as a last resort for a piece with none recorded yet."""
    if content.canonical_url:
        return content.canonical_url
    return f"{site_url}/{content.slug}" if site_url else f"urn:herald:content:{content.id}"


def build_feed(project: Project, items: list[Content], *, self_url: str) -> str:
    """RSS 2.0 XML for *project*'s published content, newest first.

    *self_url* is this feed's own absolute URL — required by the ``atom:link
    rel="self"`` convention most readers and validators expect.
    """
    site_url = (project.live_url or "").rstrip("/")

    entries = []
    for content in items:
        link = _item_link(content, site_url)
        description = content.excerpt or content.meta_description or ""
        pub_date = (
            f"<pubDate>{format_datetime(content.published_at)}</pubDate>"
            if content.published_at
            else ""
        )
        entries.append(
            "<item>"
            f"<title>{_clean(content.title)}</title>"
            f"<link>{_clean(link)}</link>"
            f'<guid isPermaLink="false">herald-content-{content.id}</guid>'
            f"<description>{_clean(description)}</description>"
            + pub_date
            + "</item>"
        )

    channel_link = site_url or self_url
    description = project.description or f"Updates from {project.name}."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{_clean(project.name)}</title>"
        f"<link>{_clean(channel_link)}</link>"
        # ``quoteattr`` rather than ``escape``, and it supplies its own quotes.
        # ``escape`` does not touch ``"`` — it is harmless in character data and
        # this is the one place in the feed that is not character data. A raw
        # quote in ``self_url`` (this is ``str(request.url)``, and a client that
        # does not percent-encode its query string can put one there) would
        # otherwise close the attribute and let the rest of the query string
        # become markup.
        f"<atom:link href={quoteattr(_ILLEGAL.sub('', self_url))} "
        'rel="self" type="application/rss+xml" />'
        f"<description>{_clean(description)}</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


__all__ = ["FEED_ITEM_LIMIT", "build_feed"]
