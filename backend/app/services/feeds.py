"""Reading RSS and Atom feeds, safely enough to point at a stranger's URL.

Almost every source a marketing team cares about already publishes a feed: a
changelog, a status page, a competitor's blog, a GitHub releases atom. So the
RSS trigger is the widest net Herald can cast without asking anyone to build an
integration — which is also why the URL is the least trustworthy input in the
system, and why three quite boring precautions are load-bearing here.

**The URL is validated like a webhook's.** It is typed into a box and Herald's
server is what opens it, so the same SSRF checks apply — loopback, private and
link-local space are refused, and redirects are not followed. See
``app.services.webhooks``, whose reasoning this borrows wholesale.

**The response is capped before it is parsed, and its DTD is refused.** The cap
(:data:`MAX_FEED_BYTES`) bounds what comes off the socket; a feed that does not
fit in two megabytes is not a feed Herald can work with anyway. It does *not*
bound what parsing that response costs, which is the whole point of the "billion
laughs" family of attacks: ``xml.etree`` expands internal entities eagerly, so a
few hundred bytes of nested ``<!ENTITY>`` declarations become gigabytes of
resident memory, and the cap the bytes passed is the cap on the *compressed*
form. :func:`_reject_dtd_entities` is what actually closes it — see there.

**Entries are identified, not counted.** Feeds reorder, republish and backfill.
A poller that remembers "I had read 10 items" writes about the same entry twice
the moment a publisher edits one. So the watermark is a set of entry ids, and
the id is the ``<guid>`` / ``<id>`` if there is one and a hash of the link and
title if there is not.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from xml.parsers import expat

import httpx

from app.config import settings
from app.services.webhooks import WebhookUrlError, validate_url

logger = logging.getLogger(__name__)

#: Hard ceiling on how much of a feed is read off the socket. See the docstring.
MAX_FEED_BYTES = 2 * 1024 * 1024

#: How many ids the watermark keeps. Comfortably more than any feed's page, so
#: an entry cannot fall off the end of the memory and be written about twice,
#: and small enough that the JSON column stays a sensible size.
SEEN_IDS_KEPT = 200

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class FeedError(RuntimeError):
    """The feed could not be fetched or made sense of."""


@dataclass(frozen=True)
class FeedEntry:
    """One item from a feed, in the fields every format agrees on."""

    #: Stable identity — guid, atom id, or a hash of link+title.
    entry_id: str
    title: str
    link: str = ""
    summary: str = ""
    published_at: datetime | None = None

    @property
    def is_empty(self) -> bool:
        """Nothing to write about: no title and no summary.

        A link alone does not count. An entry like this is skipped rather than
        handed to the generator, which would otherwise be briefed with a URL and
        invent the rest.
        """
        return not (self.title.strip() or self.summary.strip())


@dataclass(frozen=True)
class Feed:
    """A parsed feed: what it is called, and what is in it."""

    title: str
    #: Newest first, as far as the feed's own ordering can be trusted.
    entries: list[FeedEntry]


def validate_feed_url(url: str) -> str:
    """Return *url* stripped, or raise :class:`FeedError`.

    Shares the outbound-webhook validator rather than reimplementing it: the
    question ("will Herald's server open this?") is identical, and two answers
    to it would eventually disagree.
    """
    try:
        return validate_url(url)
    except WebhookUrlError as exc:
        raise FeedError(str(exc).replace("call that URL", "read that feed")) from exc


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    # Atom permits ``<title type="html">``; nested markup arrives as children,
    # so gather all descendant text rather than just ``.text``.
    return "".join(element.itertext()).strip()


def _first(parent: ElementTree.Element, *paths: str) -> ElementTree.Element | None:
    for path in paths:
        found = parent.find(path)
        if found is not None:
            return found
    return None


def _parse_date(raw: str) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom), whichever this turns out to be."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_id(guid: str, link: str, title: str) -> str:
    """The identity to remember this entry by. See the module docstring."""
    if guid.strip():
        return guid.strip()[:200]
    digest = hashlib.sha256(f"{link}\x1f{title}".encode()).hexdigest()
    return f"h:{digest[:40]}"


def _atom_link(entry: ElementTree.Element) -> str:
    """The ``rel="alternate"`` href, or the first link that has one."""
    fallback = ""
    for node in entry.findall(f"{_ATOM}link"):
        href = (node.get("href") or "").strip()
        if not href:
            continue
        rel = (node.get("rel") or "alternate").strip()
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


class _RootReached(Exception):
    """The pre-scan got as far as the root element. Nothing left to check."""


def _reject_dtd_entities(xml: str | bytes) -> None:
    """Refuse a document that declares XML entities, before anything expands one.

    ``xml.etree`` has no switch for this. Its C parser exposes neither expat's
    ``EntityDeclHandler`` nor a writable ``entity`` mapping, so the declarations
    cannot be intercepted on the way through the parse that matters — by the
    time :func:`parse` is running, a document that turns 300 bytes into a
    gigabyte has already done it.

    So the check runs first, on its own expat parser, with every handler off
    except two. ``EntityDeclHandler`` fires when an entity is *declared*, which
    is strictly before any reference to it is expanded — refusing there costs
    nothing and is what makes the guard safe rather than merely early.
    ``StartElementHandler`` aborts the scan at the root element: entity
    declarations live in the DTD, the DTD precedes the root, so once the root
    opens there is nothing left to find. That is what keeps this a scan of the
    prolog rather than a second full parse of every feed Herald reads.

    External entities are refused by the same handler, which matters for a
    different reason: expat will not *fetch* one by default, but a document that
    declares ``<!ENTITY x SYSTEM "file:///etc/passwd">`` is not a feed, and
    saying so is better than parsing it and silently yielding empty text.

    A malformed document is not this function's problem — it returns quietly and
    lets :func:`parse` raise the error that actually describes what is wrong.
    """
    parser = expat.ParserCreate()

    def _on_entity_decl(name: str, *_args: object) -> None:
        raise FeedError(
            f"That feed declares an XML entity ({name!r}). Herald does not parse "
            "feeds with a document type definition."
        )

    def _on_root(*_args: object) -> None:
        raise _RootReached

    parser.EntityDeclHandler = _on_entity_decl
    parser.StartElementHandler = _on_root
    try:
        parser.Parse(xml if isinstance(xml, bytes) else xml.encode("utf-8"), True)
    except _RootReached:
        return
    except expat.ExpatError:
        return


def parse(xml: str | bytes) -> Feed:
    """Parse RSS 2.0 or Atom into a :class:`Feed`.

    Format detection is by element name rather than by anything the document
    claims about itself: half the feeds on the internet serve Atom as
    ``text/xml`` and RSS as ``application/atom+xml``.
    """
    _reject_dtd_entities(xml)
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise FeedError(f"That is not valid XML: {exc}") from exc

    channel = root.find("channel")
    if channel is not None:
        return Feed(
            title=_text(channel.find("title")),
            entries=[_rss_entry(item) for item in channel.findall("item")],
        )

    if root.tag == f"{_ATOM}feed":
        return Feed(
            title=_text(root.find(f"{_ATOM}title")),
            entries=[_atom_entry(item) for item in root.findall(f"{_ATOM}entry")],
        )

    raise FeedError(
        "No RSS <channel> or Atom <feed> in that document — is the URL a feed?"
    )


def _rss_entry(item: ElementTree.Element) -> FeedEntry:
    title = _text(item.find("title"))
    link = _text(item.find("link"))
    guid = _text(item.find("guid"))
    # content:encoded is the full post where a publisher offers both; the
    # description is then usually a teaser.
    summary = _text(_first(item, f"{_CONTENT}encoded", "description"))
    published = _text(_first(item, "pubDate", f"{_DC}date"))
    return FeedEntry(
        entry_id=_entry_id(guid, link, title),
        title=title,
        link=link,
        summary=summary,
        published_at=_parse_date(published),
    )


def _atom_entry(item: ElementTree.Element) -> FeedEntry:
    title = _text(item.find(f"{_ATOM}title"))
    link = _atom_link(item)
    guid = _text(item.find(f"{_ATOM}id"))
    summary = _text(_first(item, f"{_ATOM}content", f"{_ATOM}summary"))
    published = _text(_first(item, f"{_ATOM}updated", f"{_ATOM}published"))
    return FeedEntry(
        entry_id=_entry_id(guid, link, title),
        title=title,
        link=link,
        summary=summary,
        published_at=_parse_date(published),
    )


def _client() -> httpx.Client:
    """The client every feed fetch goes out on.

    A function rather than an inline constructor so tests can hand :func:`fetch`
    a transport instead of a socket. ``follow_redirects=False`` for the same
    reason webhook delivery refuses redirects: a 3xx can move a validated public
    URL to somewhere inside the network, and Herald cannot tell which it is.
    """
    return httpx.Client(
        timeout=settings.feed_timeout_seconds,
        follow_redirects=False,
        headers={
            "User-Agent": f"Herald/0.1 feeds (+{settings.openrouter_app_url})",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )


def _read_capped(http: httpx.Client, target: str) -> bytes:
    """The feed body, abandoning the download once it passes the cap.

    Streamed rather than fetched whole: checking the length after ``.content``
    has already buffered a gigabyte into memory would be a check that runs
    strictly too late to help.
    """
    with http.stream("GET", target) as response:
        if response.is_redirect:
            raise FeedError(
                f"The feed returned {response.status_code} — Herald does not follow "
                "feed redirects. Use the URL it points at."
            )
        if response.status_code >= 400:
            raise FeedError(f"The feed returned {response.status_code}.")

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise FeedError(
                    f"That feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB, "
                    "which is more than Herald will parse."
                )
            chunks.append(chunk)
    return b"".join(chunks)


def fetch(url: str, *, client: httpx.Client | None = None) -> Feed:
    """Fetch and parse one feed. Raises :class:`FeedError` on any failure."""
    target = validate_feed_url(url)

    own_client = client is None
    http = client or _client()
    try:
        body = _read_capped(http, target)
    except httpx.HTTPError as exc:
        raise FeedError(f"Could not reach that feed: {type(exc).__name__}: {exc}") from exc
    finally:
        if own_client:
            http.close()

    feed = parse(body)
    logger.info("feed %s: %d entries", target, len(feed.entries))
    return feed


def new_entries(feed: Feed, seen_ids: list[str] | None) -> list[FeedEntry]:
    """The entries of *feed* that are not in *seen_ids*, newest first.

    ``seen_ids`` of ``None`` means this feed has never been polled, and the
    answer is deliberately *nothing*: a first poll of a five-year-old blog
    should record where the feed is, not write about five years of posts. That
    is the same baselining rule the GitHub scan has always used.
    """
    if seen_ids is None:
        return []
    known = set(seen_ids)
    return [e for e in feed.entries if e.entry_id not in known and not e.is_empty]


def remember(seen_ids: list[str] | None, feed: Feed) -> list[str]:
    """The watermark after seeing *feed*, newest ids first and bounded."""
    fresh = [e.entry_id for e in feed.entries]
    merged = list(dict.fromkeys(fresh + list(seen_ids or [])))
    return merged[:SEEN_IDS_KEPT]


__all__ = [
    "MAX_FEED_BYTES",
    "SEEN_IDS_KEPT",
    "Feed",
    "FeedEntry",
    "FeedError",
    "fetch",
    "new_entries",
    "parse",
    "remember",
    "validate_feed_url",
]
