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
from app.models.mixins import as_aware
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
    return f"{site_url}/{content.slug}" if site_url else f"urn:pulse:content:{content.id}"


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
        # ``as_aware`` before formatting, because the offset an RFC 822 date
        # carries is not decoration. ``format_datetime`` writes ``-0000`` for a
        # naive datetime and ``+0000`` for one that knows it is UTC, and RFC 5322
        # §3.3 gives those two spellings different meanings: ``+0000`` is "this
        # instant, in UTC", ``-0000`` is "we are not telling you the zone".
        # Pulse's timestamps are always UTC — ``utcnow`` writes every one of
        # them — so which spelling came out depended only on whether the driver
        # handed the column back with its offset attached. PostgreSQL does and
        # SQLite does not, which means the feed said one thing in production and
        # the other in every test that read it, and the tests were the ones
        # asserting it was right.
        pub_date = (
            f"<pubDate>{format_datetime(as_aware(content.published_at))}</pubDate>"
            if content.published_at
            else ""
        )
        entries.append(
            "<item>"
            f"<title>{_clean(content.title)}</title>"
            f"<link>{_clean(link)}</link>"
            f'<guid isPermaLink="false">pulse-content-{content.id}</guid>'
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
        # this is the one place in the feed that is not character data, so a raw
        # quote here would close the attribute and let whatever followed become
        # markup.
        #
        # The caller no longer passes anything a client controls: ``self_url``
        # is built from configuration (see ``app.routers.projects.project_feed``).
        # The escaping stays anyway, because a renderer that only produces valid
        # XML for the arguments its current caller happens to pass is not a
        # renderer that produces valid XML.
        f"<atom:link href={quoteattr(_ILLEGAL.sub('', self_url))} "
        'rel="self" type="application/rss+xml" />'
        f"<description>{_clean(description)}</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


__all__ = ["FEED_ITEM_LIMIT", "build_feed"]
