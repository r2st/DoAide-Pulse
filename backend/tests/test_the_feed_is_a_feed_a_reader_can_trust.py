"""The public feed, checked against what a reader actually does with it.

``test_rss.py`` covers the renderer's shape and ``test_rss_escaping.py`` covers
the one property every value in it has to keep. Neither goes through the
endpoint, so neither could see the two things this file was written for — both
of which were true of the bytes production served and of nothing the suite ran.

**The dates said "zone unknown".** ``format_datetime`` writes ``+0000`` for an
aware datetime and ``-0000`` for a naive one, and RFC 5322 §3.3 makes those
different claims: the first is "this instant, in UTC", the second is "the local
zone is not being disclosed". Every timestamp Pulse writes is UTC, so the
spelling was decided entirely by whether the driver returned the column with its
offset attached — PostgreSQL does, SQLite does not. The feed therefore said one
thing in production and the other under the tests that were checking it.

**The self link said ``http``.** ``atom:link rel="self"`` was ``str(request.url)``.
Behind Caddy, which terminates TLS and forwards plain HTTP from a bridge address
uvicorn does not have in ``forwarded_allow_ips``, that scheme is ``http`` for
every request that arrived over ``https`` — so the feed advertised itself at a
scheme the host, which is HSTS-preloaded, refuses to serve. It also carried
whatever query string the caller appended, which is not part of where the feed
lives and is not a value anybody vouched for.

The rest of the file is the RSS 2.0 contract itself, asserted through a real
parser at the endpoint: the elements a reader needs, dates it can parse, and
guids it can deduplicate on.
"""
from __future__ import annotations

from datetime import timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware, utcnow
from app.services import rss

ATOM = "{http://www.w3.org/2005/Atom}"


def _publish(db, project, *, title, when, **overrides):
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        title=title,
        slug=title.lower().replace(" ", "-"),
        excerpt=f"About {title}.",
        published_at=when,
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


def _feed(client, project, query: str = "") -> ElementTree.Element:
    resp = client.get(f"/api/v1/projects/{project.id}/feed.xml{query}")
    assert resp.status_code == 200, resp.text
    return ElementTree.fromstring(resp.content)


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #


def test_a_pub_date_says_utc_rather_than_declining_to_say(client, db, project):
    when = utcnow()
    _publish(db, project, title="Dated", when=when)

    raw = _feed(client, project).find("channel/item/pubDate").text

    # `-0000` is a valid RFC 5322 date and a *different* claim: no zone given.
    assert raw.endswith("+0000")


def test_a_pub_date_round_trips_to_the_instant_it_was_published(client, db, project):
    """The date is not merely well-formed — it names the right moment.

    Parsed back with the same library a reader would use, and compared against
    the stored value rather than against a string. A second of slack for the
    format's own resolution, which does not carry microseconds.
    """
    when = utcnow()
    _publish(db, project, title="Dated", when=when)

    parsed = parsedate_to_datetime(_feed(client, project).find("channel/item/pubDate").text)

    assert abs(parsed - as_aware(when)) < timedelta(seconds=1)


def test_a_piece_with_no_publication_date_simply_has_no_pub_date(client, db, project):
    """RSS 2.0 makes ``pubDate`` optional, and an empty or invented one is worse
    than an absent one — a reader sorts on it."""
    _publish(db, project, title="Undated", when=None)

    assert _feed(client, project).find("channel/item/pubDate") is None


# --------------------------------------------------------------------------- #
# Where the feed says it lives                                                 #
# --------------------------------------------------------------------------- #


def test_the_self_link_is_the_public_url_of_this_feed(client, db, project):
    _publish(db, project, title="Anything", when=utcnow())

    href = _feed(client, project).find(f"channel/{ATOM}link").attrib["href"]

    assert href == (
        f"{settings.api_base_url}/api/v1/projects/{project.id}/feed.xml"
    )


def test_the_self_link_does_not_depend_on_how_the_request_reached_us(
    client, db, project
):
    """Which is the bug: behind a TLS-terminating proxy the request's own scheme
    is ``http``, and the feed was repeating it back."""
    _publish(db, project, title="Anything", when=utcnow())

    plain = _feed(client, project).find(f"channel/{ATOM}link").attrib["href"]
    forwarded = client.get(
        f"/api/v1/projects/{project.id}/feed.xml",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "elsewhere.test"},
    )

    assert ElementTree.fromstring(forwarded.content).find(
        f"channel/{ATOM}link"
    ).attrib["href"] == plain


def test_a_query_string_the_caller_appended_stays_out_of_the_feed(client, db, project):
    """It is not part of where the feed lives, and it is not a value the feed
    should be quoting to every subscriber."""
    _publish(db, project, title="Anything", when=utcnow())

    xml = client.get(
        f"/api/v1/projects/{project.id}/feed.xml?utm_source=reader&q=%22"
    ).text

    assert "utm_source" not in xml


def test_the_channel_link_is_the_project_site_when_there_is_one(client, db, project):
    _publish(db, project, title="Anything", when=utcnow())

    assert _feed(client, project).find("channel/link").text == project.live_url


def test_the_channel_link_falls_back_to_the_feed_itself(client, db, project):
    """A project with no site still has to name *somewhere* — RSS 2.0 requires
    ``link`` on the channel, and an empty one fails validation."""
    project.live_url = None
    db.commit()
    _publish(db, project, title="Anything", when=utcnow())

    channel_link = _feed(client, project).find("channel/link").text
    assert channel_link.endswith(f"/projects/{project.id}/feed.xml")


# --------------------------------------------------------------------------- #
# The RSS 2.0 contract                                                         #
# --------------------------------------------------------------------------- #


def test_the_channel_carries_every_element_rss_requires(client, db, project):
    _publish(db, project, title="Anything", when=utcnow())

    root = _feed(client, project)
    assert root.tag == "rss"
    assert root.attrib["version"] == "2.0"
    channel = root.find("channel")
    for required in ("title", "link", "description"):
        element = channel.find(required)
        assert element is not None, required
        assert (element.text or "").strip(), f"{required} is present but empty"


def test_an_item_carries_a_title_a_link_and_a_guid(client, db, project):
    _publish(
        db, project, title="Released", when=utcnow(),
        canonical_url="https://dev.to/herald/released",
    )

    item = _feed(client, project).find("channel/item")
    assert item.find("title").text == "Released"
    assert item.find("link").text == "https://dev.to/herald/released"
    guid = item.find("guid")
    # isPermaLink=false: the guid is Pulse's own identifier, not a URL. A
    # reader that took it for one would fetch a page that does not exist.
    assert guid.attrib["isPermaLink"] == "false"
    assert guid.text


def test_no_two_items_share_a_guid(client, db, project):
    """It is what a reader deduplicates on. Two items with one guid is one item
    on the subscriber's screen, and which of the two is a coin toss."""
    for n in range(5):
        _publish(db, project, title=f"Piece {n}", when=utcnow())

    guids = [g.text for g in _feed(client, project).findall("channel/item/guid")]
    assert len(set(guids)) == len(guids) == 5


def test_a_guid_does_not_change_between_two_polls_of_the_same_piece(
    client, db, project
):
    """A guid that moved would re-announce every old post as new on every poll."""
    piece = _publish(db, project, title="Stable", when=utcnow())

    first = _feed(client, project).find("channel/item/guid").text
    # Everything about the piece a reader can see, edited underneath it.
    piece.title = "Stable, retitled"
    piece.excerpt = "Rewritten."
    db.commit()

    assert _feed(client, project).find("channel/item/guid").text == first


def test_items_come_newest_first(client, db, project):
    now = utcnow()
    for n in range(4):
        _publish(db, project, title=f"Piece {n}", when=now - timedelta(days=n))

    titles = [t.text for t in _feed(client, project).findall("channel/item/title")]
    assert titles == ["Piece 0", "Piece 1", "Piece 2", "Piece 3"]


def test_two_pieces_published_at_the_same_instant_still_have_an_order(
    client, db, project
):
    """A sort with ties is not a sort. It matters at the ``FEED_ITEM_LIMIT``
    boundary, where which of the tied rows makes the cut would otherwise be the
    database's choice and free to differ between two polls — a reader watching
    an item appear, vanish and come back as new."""
    same = utcnow()
    for n in range(3):
        _publish(db, project, title=f"Piece {n}", when=same)

    first = [t.text for t in _feed(client, project).findall("channel/item/title")]
    second = [t.text for t in _feed(client, project).findall("channel/item/title")]
    assert first == second
    # Newest first still, decided by id where the dates cannot decide.
    assert first == ["Piece 2", "Piece 1", "Piece 0"]


def test_the_feed_is_capped_rather_than_growing_with_the_archive(
    client, db, project, monkeypatch
):
    monkeypatch.setattr(rss, "FEED_ITEM_LIMIT", 3)
    now = utcnow()
    for n in range(6):
        _publish(db, project, title=f"Piece {n}", when=now - timedelta(hours=n))

    items = _feed(client, project).findall("channel/item")
    assert len(items) == 3
    # The newest three, not an arbitrary three.
    assert [i.find("title").text for i in items] == ["Piece 0", "Piece 1", "Piece 2"]


@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("Ünïcödé & emoji 🎉", "non-ASCII survives the declared encoding"),
        ("Pulse <1.0> & \"friends\"", "markup characters are escaped, not stripped"),
        ("A title with\ta tab", "whitespace XML can carry is kept"),
    ],
)
def test_a_title_survives_the_round_trip_through_the_response(
    client, db, project, title, why
):
    """Parsed from ``resp.content`` — the bytes — rather than from ``resp.text``,
    so the declared ``encoding="UTF-8"`` is part of what is being asserted."""
    _publish(db, project, title=title, when=utcnow())

    assert _feed(client, project).find("channel/item/title").text == title, why
