"""What the feed parser does with XML that was written to hurt it.

``test_feeds.py`` covers feeds that are merely *wrong* — truncated, empty, the
wrong format, a date nobody can parse. This file covers feeds that are hostile,
which is a different question: the RSS trigger points Herald's own worker at a
URL a user typed, so a feed body is attacker-controlled input that arrives with
no signature, no size the user vouched for, and no reason to be well-meaning.

The attack that matters is entity expansion. ``xml.etree`` resolves internal
entities eagerly, so nesting a handful of declarations turns a few hundred bytes
into as much resident memory as the attacker cares to name — and the
:data:`MAX_FEED_BYTES` cap does not help, because it bounds the bytes read off
the socket and the whole point of the attack is that those bytes are small. The
guard has to refuse the declaration itself, which is what
:func:`app.services.feeds._reject_dtd_entities` does.

The expansion assertions below are written against the *declared* size rather
than a measured allocation: a test that allocates the bomb to prove the bomb
allocates is a test that hangs the suite when it regresses.
"""
from __future__ import annotations

import pytest

from app.services import feeds
from app.services.feeds import FeedError

WELL_FORMED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Changelog</title>
<item><title>v2</title><link>https://example.com/v2</link>
<guid>https://example.com/v2</guid></item>
</channel></rss>"""


# --------------------------------------------------------------------------- #
# Entity expansion                                                             #
# --------------------------------------------------------------------------- #


def _bomb(levels: int, *, width: int = 10) -> str:
    """A billion-laughs document with *levels* of nesting.

    Each level references the one below it *width* times, so the root entity
    expands to ``width ** levels`` copies of the seed. Four levels is already
    enough to prove the mechanism without asking anything to allocate it.
    """
    seed = "a" * 50
    decls = [f'<!ENTITY e0 "{seed}">']
    for level in range(1, levels):
        decls.append(f'<!ENTITY e{level} "{f"&e{level - 1};" * width}">')
    return (
        '<?xml version="1.0"?>\n'
        f"<!DOCTYPE rss [{''.join(decls)}]>\n"
        f"<rss><channel><title>&e{levels - 1};</title></channel></rss>"
    )


def test_a_feed_that_declares_entities_is_refused_before_anything_expands():
    """The declaration is the thing refused, not the expansion.

    Three levels at width ten is 1000 copies of a 50-byte seed — 50 KB from
    about 200 bytes of input, and the ratio is exponential in the nesting. The
    document is well under :data:`~app.services.feeds.MAX_FEED_BYTES`, which is
    the point: the size cap can never see this coming.
    """
    document = _bomb(3)
    assert len(document) < 1024

    with pytest.raises(FeedError) as exc:
        feeds.parse(document)

    assert "entity" in str(exc.value).lower()


def test_the_refusal_does_not_depend_on_how_deeply_the_bomb_is_nested():
    """One level or nine, it is refused at the first declaration either way."""
    for levels in (1, 4, 9):
        with pytest.raises(FeedError):
            feeds.parse(_bomb(levels))


def test_an_external_entity_declaration_is_refused():
    """``SYSTEM`` entities never reach the parser that would resolve them.

    expat will not fetch one by default, so this is not a live file-read. It is
    refused anyway: a document reaching for ``/etc/passwd`` is not a feed, and
    saying so beats parsing it into an item with a mysteriously empty title.
    """
    document = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
        "<rss><channel><title>&secret;</title></channel></rss>"
    )
    with pytest.raises(FeedError) as exc:
        feeds.parse(document)

    assert "entity" in str(exc.value).lower()


def test_a_parameter_entity_declaration_is_refused_too():
    """``<!ENTITY % ...>`` is the same hazard wearing the DTD's syntax."""
    document = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss [<!ENTITY % pe "<!ENTITY inner \'x\'>"> %pe;]>'
        "<rss><channel><title>t</title></channel></rss>"
    )
    with pytest.raises(FeedError):
        feeds.parse(document)


def test_the_guard_accepts_bytes_as_well_as_text():
    """:func:`~app.services.feeds.fetch` hands ``parse`` the raw response body."""
    with pytest.raises(FeedError):
        feeds.parse(_bomb(3).encode("utf-8"))


# --------------------------------------------------------------------------- #
# What the guard must not break                                                #
# --------------------------------------------------------------------------- #


def test_an_ordinary_feed_still_parses():
    feed = feeds.parse(WELL_FORMED)
    assert feed.title == "Changelog"
    assert [e.title for e in feed.entries] == ["v2"]


def test_the_five_predefined_entities_are_not_declarations():
    """``&amp;`` and friends are built into XML — no DTD declares them.

    Worth pinning separately: a guard that refused entity *references* rather
    than entity *declarations* would reject most of the real feeds on the
    internet, and every one of them would fail with a security error.
    """
    document = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Tom &amp; Jerry &lt;live&gt; &quot;now&quot; &apos;here&apos;</title>"
        "</channel></rss>"
    )
    assert feeds.parse(document).title == "Tom & Jerry <live> \"now\" 'here'"


def test_numeric_character_references_still_resolve():
    document = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>caf&#233; &#x2014; open</title></channel></rss>"
    )
    assert feeds.parse(document).title == "café — open"


def test_a_doctype_without_an_internal_subset_is_allowed():
    """Plenty of real feeds carry a bare ``<!DOCTYPE>`` and declare nothing.

    The guard is about declarations, so this one goes through — expat does not
    fetch the external DTD, and no entity is ever defined.
    """
    document = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss PUBLIC "-//Netscape//DTD RSS 0.91//EN" '
        '"http://my.netscape.com/publish/formats/rss-0.91.dtd">'
        "<rss version=\"0.91\"><channel><title>Old</title></channel></rss>"
    )
    assert feeds.parse(document).title == "Old"


def test_the_words_entity_and_doctype_in_a_post_body_are_just_words():
    """The guard parses the prolog; it does not grep the document.

    A changelog entry about XML parsing contains the literal text the guard
    looks for. A substring check would refuse it — this is the test that says
    the implementation is not allowed to become one.
    """
    document = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>XML notes</title>'
        "<item><title>On &lt;!ENTITY&gt; and &lt;!DOCTYPE&gt;</title>"
        "<description>Why we refuse &lt;!ENTITY a &quot;bomb&quot;&gt; in feeds."
        "</description><guid>x1</guid></item></channel></rss>"
    )
    feed = feeds.parse(document)
    assert feed.entries[0].title == "On <!ENTITY> and <!DOCTYPE>"


def test_malformed_xml_still_reports_as_malformed_not_as_an_entity():
    """The pre-scan hands a broken document back rather than claiming a bomb.

    Both paths raise :class:`FeedError`, so the distinction only exists in the
    message — which is the only thing the user sees when their feed URL is
    actually pointed at an HTML error page.
    """
    with pytest.raises(FeedError) as exc:
        feeds.parse("<html><body>404 Not Found</body></html>")

    assert "entity" not in str(exc.value).lower()
