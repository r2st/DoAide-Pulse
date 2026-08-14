"""Values that would end the feed early if they went out unescaped.

``test_rss.py`` covers the shape of the feed. This file covers the one property
that has to hold for every value in it: whatever a title, a description or a URL
contains, the result still parses. The feed is a single document, so this is not
a per-item concern — one bad character in one title takes the whole feed down for
every subscriber, and the items after it simply stop arriving.

Three inputs reach the renderer and none of them are trusted:

* **Titles and descriptions** are model output, then edited by hand. They carry
  angle brackets and ampersands as a matter of course.
* **Links** are ``canonical_url`` as returned by whichever platform published
  the piece.
* ``self_url`` is the only value that lands in an *attribute*, and attributes
  are the only place a bare ``"`` is markup. It was ``str(request.url)`` when
  this file was written — whatever the client put on the wire — and the endpoint
  now builds it from configuration instead, for reasons that have nothing to do
  with escaping (see ``test_the_feed_is_a_feed_a_reader_can_trust``). The tests
  below still pass it hostile values, because they are tests of the renderer:
  one that only produces valid XML for the arguments its current caller happens
  to pass is not a renderer that produces valid XML.

Every assertion here runs the output through a real XML parser rather than
matching strings: "the quote was escaped" is a claim about the bytes, but "the
feed still parses" is the claim that actually matters.
"""
from __future__ import annotations

from xml.etree import ElementTree

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone
from app.services import rss

SELF = "https://herald.example.com/api/v1/projects/1/feed.xml"


def _project(**overrides) -> Project:
    defaults = dict(
        id=1,
        user_id=1,
        name="Herald",
        slug="herald",
        description="Updates.",
        live_url="https://herald.example.com",
        tone=Tone.TECHNICAL,
    )
    defaults.update(overrides)
    return Project(**defaults)


def _content(**overrides) -> Content:
    defaults = dict(
        id=1,
        project_id=1,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        title="Release",
        slug="release",
        excerpt="Notes.",
    )
    defaults.update(overrides)
    return Content(**defaults)


# --------------------------------------------------------------------------- #
# self_url lands in an attribute                                               #
# --------------------------------------------------------------------------- #


def test_a_quote_in_the_request_url_cannot_close_the_atom_link_attribute():
    """The injection this escaping exists to stop.

    ``escape()`` does not touch ``"`` — correctly, since it is ordinary
    character data everywhere else in the feed. ``href`` is the one attribute
    here, and a client is free to send a query string with a literal quote in
    it: nothing percent-encodes on the way in, and the URL arrives as typed.
    Unescaped, everything after that quote stops being an attribute value.
    """
    hostile = f'{SELF}?q="><script>alert(1)</script><x y="'
    xml = rss.build_feed(_project(), [], self_url=hostile)

    root = ElementTree.fromstring(xml)  # the assertion: this does not raise
    atom = root.find("channel/{http://www.w3.org/2005/Atom}link")
    assert atom is not None
    # Round-tripped intact rather than mangled — escaping, not stripping.
    assert atom.attrib["href"] == hostile
    assert root.find("channel/script") is None


def test_ampersands_and_angle_brackets_in_the_request_url_survive():
    hostile = f"{SELF}?a=1&b=2&c=<x>"
    xml = rss.build_feed(_project(), [], self_url=hostile)

    root = ElementTree.fromstring(xml)
    atom = root.find("channel/{http://www.w3.org/2005/Atom}link")
    assert atom.attrib["href"] == hostile


def test_the_request_url_is_also_the_channel_link_when_the_project_has_no_site():
    """``channel_link`` falls back to ``self_url``, so it inherits the problem."""
    hostile = f'{SELF}?q="&<>'
    xml = rss.build_feed(_project(live_url=None), [], self_url=hostile)

    root = ElementTree.fromstring(xml)
    assert root.find("channel/link").text == hostile


# --------------------------------------------------------------------------- #
# Characters XML cannot represent at all                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "char, name",
    [
        ("\x00", "NUL"),
        ("\x08", "backspace"),
        ("\x0c", "form feed"),
        ("\x1b", "escape"),
        ("\x7f", "delete"),
        ("￾", "noncharacter"),
    ],
)
def test_a_control_character_in_a_title_does_not_break_the_whole_feed(char, name):
    """These have no XML representation — not even as ``&#0;``.

    A title picks one up from a copy-paste or a model that emitted a stray byte.
    Escaping cannot help, because there is nothing to escape them *to*, so they
    are dropped. The item survives; without this the entire document is
    unparseable and every other item disappears with it.
    """
    content = _content(title=f"Release{char}Notes")
    xml = rss.build_feed(_project(), [content], self_url=SELF)

    root = ElementTree.fromstring(xml)
    assert root.find("channel/item/title").text == "ReleaseNotes", name


def test_tabs_and_newlines_are_kept():
    """The three control characters XML does allow are not casualties of the scrub."""
    content = _content(excerpt="line one\nline two\ttabbed\r\nend")
    xml = rss.build_feed(_project(), [content], self_url=SELF)

    root = ElementTree.fromstring(xml)
    text = root.find("channel/item/description").text
    assert "line one" in text and "line two" in text and "\t" in text


def test_a_control_character_in_the_project_name_is_dropped_too():
    xml = rss.build_feed(_project(name="Her\x00ald"), [], self_url=SELF)

    root = ElementTree.fromstring(xml)
    assert root.find("channel/title").text == "Herald"


def test_a_control_character_in_the_request_url_is_dropped():
    """``self_url`` gets the same scrub, and it is in an attribute.

    Attribute values are more restrictive than character data, not less, so the
    one value that is not run through the shared helper still needs it.
    """
    xml = rss.build_feed(_project(), [], self_url=f"{SELF}?q=a\x00b")

    root = ElementTree.fromstring(xml)
    atom = root.find("channel/{http://www.w3.org/2005/Atom}link")
    assert atom.attrib["href"] == f"{SELF}?q=ab"


# --------------------------------------------------------------------------- #
# The ordinary case                                                            #
# --------------------------------------------------------------------------- #


def test_markup_in_a_title_is_escaped_not_stripped():
    """A post about HTML keeps its angle brackets — they just stop being markup."""
    content = _content(
        title="Why <script> tags & you",
        excerpt='Use <div class="x"> sparingly.',
    )
    xml = rss.build_feed(_project(), [content], self_url=SELF)

    root = ElementTree.fromstring(xml)
    item = root.find("channel/item")
    assert item.find("title").text == "Why <script> tags & you"
    assert item.find("description").text == 'Use <div class="x"> sparingly.'
    # Escaped, so the parser sees text — not an element it had to recover from.
    assert root.find("channel/item/script") is None


def test_a_hostile_canonical_url_from_a_platform_is_escaped():
    """``link`` is whatever the publishing platform handed back."""
    content = _content(canonical_url='https://x.example/p?a=1&b=<2>&c="3"')
    xml = rss.build_feed(_project(), [content], self_url=SELF)

    root = ElementTree.fromstring(xml)
    assert root.find("channel/item/link").text == 'https://x.example/p?a=1&b=<2>&c="3"'


def test_every_hostile_value_at_once_still_yields_one_parseable_document():
    """The combination, since these are separate call sites in one f-string."""
    project = _project(name="A & B <c>", description="d\x00e & <f>")
    items = [
        _content(id=n, title=f"<t{n}> & \x01x", excerpt=f'"{n}" & <e>',
                 canonical_url=f"https://x.example/{n}?a=&b=<>")
        for n in range(1, 4)
    ]
    xml = rss.build_feed(project, items, self_url=f'{SELF}?q="&<>\x00')

    root = ElementTree.fromstring(xml)
    assert len(root.findall("channel/item")) == 3
    assert root.find("channel/title").text == "A & B <c>"
    assert root.find("channel/description").text == "de & <f>"
