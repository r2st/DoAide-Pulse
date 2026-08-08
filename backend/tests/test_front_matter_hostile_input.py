"""Front matter that survives whatever the model put in the title.

The Git publisher commits a Markdown file into the user's own blog repo, and
the top of that file is a YAML block built from values nobody vetted: the
title, the meta description, the keyword list. All of it is model-written or
hand-edited free text, and all of it reaches ``formatting.front_matter``.

A YAML block that does not parse is not a cosmetic problem there — it is the
user's site build failing on a commit Herald made. So the assertions here are
mostly round-trips through a real YAML parser rather than string matches: what
matters is that the value comes back out exactly as it went in, whatever it
contained.

The three failure modes, all of which the escaping used to have:

* a trailing backslash escaping the closing quote, so the scalar never ends;
* a raw quote inside a flow sequence, ending the item early;
* a newline carrying a ``---`` into a block that ``---`` delimits.
"""
from __future__ import annotations

import pytest

from app.services.publishers import formatting
from app.services.publishers.base import PublishRequest
from app.services.publishers.git import GitAdapter

yaml = pytest.importorskip("yaml")

BACKSLASH = chr(92)


def parse(block: str) -> dict:
    """The front-matter block as YAML sees it, delimiters stripped."""
    lines = block.splitlines()
    assert lines[0] == "---" and lines[-1] == "---"
    return yaml.safe_load("\n".join(lines[1:-1])) or {}


# --------------------------------------------------------------------------- #
# Scalars                                                                      #
# --------------------------------------------------------------------------- #

#: Each of these used to either fail to parse or come back changed.
HOSTILE_SCALARS = [
    pytest.param("Windows path C:" + BACKSLASH, id="trailing-backslash"),
    pytest.param("Use " + BACKSLASH + "n to break lines", id="literal-backslash-n"),
    pytest.param("regex " + BACKSLASH * 2 + "d+ matches", id="doubled-backslash"),
    pytest.param('A "quoted" title', id="quotes"),
    pytest.param('mixed ' + BACKSLASH + '" escape', id="backslash-then-quote"),
    pytest.param("Hello\n---\npublished: false", id="newline-and-delimiter"),
    pytest.param("tab\there", id="tab"),
    pytest.param("carriage\rreturn", id="carriage-return"),
    pytest.param("null\x00byte", id="nul"),
    pytest.param("bell\x07char", id="c0-control"),
    pytest.param("next\x85line", id="c1-next-line"),
    pytest.param("line\u2028separator", id="unicode-line-separator"),
    pytest.param("para\u2029separator", id="unicode-paragraph-separator"),
    pytest.param("em — dash and “smart quotes”", id="non-ascii-passes-through"),
    pytest.param("emoji 🚀 survives", id="astral-plane"),
]


@pytest.mark.parametrize("value", HOSTILE_SCALARS)
def test_a_scalar_round_trips_through_a_real_yaml_parser(value):
    assert parse(formatting.front_matter({"title": value})) == {"title": value}


@pytest.mark.parametrize("value", HOSTILE_SCALARS)
def test_a_list_item_round_trips_through_a_real_yaml_parser(value):
    # The flow sequence used to be the unescaped branch entirely: `keywords`
    # reaches it straight from the content record, and unlike `tags` it is
    # never normalized down to an alphanumeric vocabulary first.
    block = formatting.front_matter({"keywords": [value, "safe"]})
    assert parse(block) == {"keywords": [value, "safe"]}


def test_a_newline_cannot_close_the_block_early():
    # The one that is an injection rather than a crash: the block is delimited
    # by `---` lines, so a value that emits one would end the front matter and
    # spill everything after it into the document as keys.
    block = formatting.front_matter(
        {"title": "Innocent\n---\ndraft: false\nauthor: attacker"}
    )
    # `---` may still appear *within* the escaped scalar; what it must not do
    # is stand alone on a line, which is the only form that delimits. Exactly
    # two such lines: the block's own open and close.
    assert [line for line in block.splitlines() if line.strip() == "---"] == ["---"] * 2
    # And the value is one line, so nothing after it can be read as a key.
    assert len(block.splitlines()) == 3
    parsed = parse(block)
    assert set(parsed) == {"title"}
    assert parsed["title"] == "Innocent\n---\ndraft: false\nauthor: attacker"


def test_the_escaper_never_double_escapes():
    # A `str.replace` chain that handles the backslash after the quote turns
    # `"` into `\\"` — the escape escaped, and the quote loose again.
    assert formatting.escape_yaml_scalar('"') == BACKSLASH + '"'
    assert formatting.escape_yaml_scalar(BACKSLASH) == BACKSLASH * 2
    assert formatting.escape_yaml_scalar(BACKSLASH + '"') == BACKSLASH * 3 + '"'


def test_escaping_leaves_ordinary_text_alone():
    plain = "Automating developer marketing"
    assert formatting.escape_yaml_scalar(plain) == plain


def test_non_string_values_are_still_quoted_and_parseable():
    # `date` arrives as an ISO string, but nothing stops a caller passing a
    # number, and `str()` on it must not produce something unquoted.
    parsed = parse(formatting.front_matter({"readingTime": 4, "wordCount": 812}))
    # Quoted, so they come back as strings — which is what the existing
    # behaviour was for every non-bool value, and what themes already read.
    assert parsed == {"readingTime": "4", "wordCount": "812"}


def test_booleans_stay_real_yaml_booleans():
    # `draft: true` has to be a boolean, not the string "true": a theme that
    # checks truthiness would treat the string "false" as a published post.
    assert parse(formatting.front_matter({"draft": True})) == {"draft": True}
    assert parse(formatting.front_matter({"live": False})) == {"live": False}


def test_empty_values_are_still_omitted():
    block = formatting.front_matter(
        {"title": "Kept", "blank": "", "none": None, "empty": []}
    )
    assert parse(block) == {"title": "Kept"}


# --------------------------------------------------------------------------- #
# The file the Git publisher actually commits                                  #
# --------------------------------------------------------------------------- #


def test_the_committed_file_parses_when_every_field_is_hostile():
    """End to end: the whole file, not just the helper.

    This is the case that matters — a post whose title, description and
    keywords all carry something awkward still produces a file a static-site
    generator can read.
    """
    title = 'Shipping ' + BACKSLASH + 'n "safely" on C:' + BACKSLASH
    request = PublishRequest(
        title=title,
        body_markdown="## Why\n\nBecause the build should not break.\n",
        excerpt='An excerpt with "quotes" and a ' + BACKSLASH + " backslash.",
        meta_description="Description\nwith a newline and ---.",
        tags=["python", "dev tools"],
        keywords=['say "hi"', "path" + BACKSLASH, "plain"],
        focus_keyword='focus "word"',
        canonical_url="https://herald.example.com/blog/shipping",
        project_name="Herald",
        slug="shipping",
    )

    contents = GitAdapter().build_file(request)

    # The block is everything up to the closing delimiter; the body follows.
    _, _, rest = contents.partition("---\n")
    block, _, body = rest.partition("\n---")
    parsed = yaml.safe_load(block)

    assert parsed["title"] == title
    assert parsed["keywords"] == ['say "hi"', "path" + BACKSLASH, "plain"]
    assert parsed["focusKeyword"] == 'focus "word"'
    assert parsed["description"] == "Description\nwith a newline and ---."
    # The body is still the body — nothing from the front matter leaked past
    # the delimiter, and nothing from the block was swallowed by it.
    assert "## Why" in body
    assert "Because the build should not break." in body


def test_a_hostile_title_cannot_inject_a_front_matter_key():
    request = PublishRequest(
        title="Post\n---\ndraft: true\nredirect: https://evil.example.com",
        body_markdown="Body.",
        excerpt="Excerpt.",
        meta_description="A description.",
        slug="post",
    )
    contents = GitAdapter().build_file(request)
    _, _, rest = contents.partition("---\n")
    block, _, _ = rest.partition("\n---")
    parsed = yaml.safe_load(block)

    assert "redirect" not in parsed
    # `draft` is only ever set by `as_draft`, which this request did not ask
    # for. A title that could add it would publish-or-unpublish by string.
    assert "draft" not in parsed
