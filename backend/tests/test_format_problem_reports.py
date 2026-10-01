"""What the editor tells an author is wrong with a thread or a changelog.

``normalize`` rewrites; ``problems`` only reports. These are the report arms —
the ones that fire on a post over the character limit, a section with more
entries than Pulse will keep, and the tokeniser's odd inputs underneath. An
unreported problem here becomes a silently truncated post at publish time.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentType
from app.services import formats

# --------------------------------------------------------------------------- #
# Threads                                                                     #
# --------------------------------------------------------------------------- #


def test_a_post_over_the_limit_is_named_with_its_length():
    long_post = "x" * 400
    body = f"First post.\n\n{long_post}"

    problems = formats.thread_problems(body, limit=280)

    assert any(p == "Post 2 is 400 characters. The limit is 280." for p in problems)


def test_problems_dispatches_on_the_shape_the_content_type_implies():
    thread = formats.problems("only one post", ContentType.SOCIAL_THREAD)
    changelog = formats.problems("no headings here", ContentType.CHANGELOG)
    article = formats.problems("anything at all", ContentType.HOW_TO)

    assert any("at least" in p for p in thread)
    assert any("at least one entry" in p for p in changelog)
    assert article == []


def test_the_tokeniser_keeps_a_double_stop_with_its_sentence():
    """"Wait.. What now?" is two tokens, not two plus an empty one.

    The split point is whitespace *after* a stop, so the second dot of ``..``
    is not one — the run stays attached to the sentence that owns it.
    """
    tokens = formats._tokens("Wait.. What now?", 100)

    assert tokens == ["Wait..", "What now?"]


# --------------------------------------------------------------------------- #
# Changelogs                                                                  #
# --------------------------------------------------------------------------- #


def test_a_section_past_the_entry_cap_says_how_many_will_be_kept():
    entries = "\n".join(
        f"- entry {i}" for i in range(formats.MAX_ENTRIES_PER_SECTION + 3)
    )
    body = f"## Fixed\n{entries}\n"

    problems = formats.changelog_problems(body)

    assert any(
        f"Fixed has {formats.MAX_ENTRIES_PER_SECTION + 3} entries" in p
        for p in problems
    )
    assert any(f"first {formats.MAX_ENTRIES_PER_SECTION} will be kept" in p for p in problems)


@pytest.mark.parametrize("heading", ["", "   ", "###", "123", "!!!"])
def test_a_heading_with_no_letters_in_it_names_no_section(heading):
    assert formats.canonical_section(heading) is None


def test_the_known_sections_and_their_aliases_still_resolve():
    assert formats.canonical_section("fixed") == "Fixed"
    assert formats.canonical_section("  ADDED  ") == "Added"
