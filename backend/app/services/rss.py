"""RSS 2.0 feed generation for a project's published content.

Public and unauthenticated by design — an RSS reader has no bearer token to
send, and everything in the feed is already live on whatever platform it was
published to. Only ``ContentStatus.PUBLISHED`` rows are ever included: a draft
or review-queue item leaking into a public feed would be a bigger problem than
any RSS feature is worth.
"""
from __future__ import annotations

from email.utils import format_datetime
from xml.sax.saxutils import escape

from app.models.content import Content
from app.models.project import Project

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
            f"<title>{escape(content.title)}</title>"
            f"<link>{escape(link)}</link>"
            f'<guid isPermaLink="false">herald-content-{content.id}</guid>'
            f"<description>{escape(description)}</description>"
            + pub_date
            + "</item>"
        )

    channel_link = site_url or self_url
    description = project.description or f"Updates from {project.name}."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{escape(project.name)}</title>"
        f"<link>{escape(channel_link)}</link>"
        f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />'
        f"<description>{escape(description)}</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


__all__ = ["FEED_ITEM_LIMIT", "build_feed"]
