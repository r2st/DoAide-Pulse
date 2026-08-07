"""Output shapes: threads and changelogs, and the repair pass that saves them."""
from __future__ import annotations

import pytest

from app.models.content import ContentType
from app.services import formats
from app.services.formats import ContentFormat

#: One content type per shape, so a test can say "any changelog" without caring
#: which type produced it.
_A_TYPE_OF: dict[ContentFormat, ContentType] = {
    ContentFormat.ARTICLE: ContentType.TUTORIAL,
    ContentFormat.THREAD: ContentType.SOCIAL_THREAD,
    ContentFormat.CHANGELOG: ContentType.CHANGELOG,
}


# --------------------------------------------------------------------------- #
# Which shape a type produces                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (ContentType.TUTORIAL, ContentFormat.ARTICLE),
        (ContentType.ANNOUNCEMENT, ContentFormat.ARTICLE),
        (ContentType.SOCIAL_THREAD, ContentFormat.THREAD),
        (ContentType.CHANGELOG, ContentFormat.CHANGELOG),
    ],
)
def test_a_type_maps_to_its_shape(content_type, expected):
    assert formats.format_of(content_type) == expected


def test_a_type_takes_its_string_spelling_too():
    assert formats.format_of("social_thread") == ContentFormat.THREAD


def test_an_unknown_type_is_treated_as_an_article():
    """Adding a content type should not require choosing a shape."""
    assert formats.format_of("whatever_comes_next") == ContentFormat.ARTICLE


def test_every_shape_has_a_rule_set_and_a_thinness_test():
    """A shape the generator cannot prompt for or judge is not usable."""
    for shape in ContentFormat:
        assert formats.PROMPT_RULES[shape].strip()
        # Empty is too thin in every shape; that it answers at all is the point.
        assert formats.too_thin_to_store("", _A_TYPE_OF[shape])


# --------------------------------------------------------------------------- #
# Threads                                                                      #
# --------------------------------------------------------------------------- #


def test_posts_are_separated_by_blank_lines():
    assert formats.parse_thread("First post.\n\nSecond post.") == [
        "First post.",
        "Second post.",
    ]


def test_a_posts_own_line_breaks_survive():
    """A two-line post with a snippet in it is a normal thing to write."""
    assert formats.parse_thread("Line one\nLine two\n\nNext") == [
        "Line one\nLine two",
        "Next",
    ]


@pytest.mark.parametrize(
    "prefix", ["1/ ", "1/5 ", "2. ", "3) ", "- ", "## "]
)
def test_numbering_and_markdown_scaffolding_is_stripped(prefix):
    """Herald numbers the thread at publish; two schemes is worse than either."""
    assert formats.parse_thread(f"{prefix}The post") == ["The post"]


def test_an_over_long_post_is_split_at_a_sentence_boundary():
    long_post = "First sentence. " + "word " * 80 + "Last bit."

    parts = formats.split_post(long_post)

    assert len(parts) > 1
    assert all(len(part) <= formats.THREAD_POST_LIMIT for part in parts)
    assert parts[0].startswith("First sentence.")


def test_splitting_loses_no_words():
    long_post = " ".join(f"word{i}" for i in range(120))

    parts = formats.split_post(long_post)

    assert " ".join(parts).split() == long_post.split()


def test_a_single_word_longer_than_the_limit_is_cut_rather_than_dropped():
    """A 400-character URL is unpublishable whole; losing it entirely is worse."""
    parts = formats.split_post("x" * 400)

    assert "".join(parts) == "x" * 400
    assert all(len(part) <= formats.THREAD_POST_LIMIT for part in parts)


def test_a_post_that_fits_is_returned_untouched():
    assert formats.split_post("Short.") == ["Short."]


def test_normalizing_repairs_a_thread_rather_than_rejecting_it():
    body = "Hook.\n\n" + "y" * 340

    result = formats.normalize_thread(body)
    posts = formats.parse_thread(result)

    assert len(posts) == 3
    assert all(len(post) <= formats.THREAD_POST_LIMIT for post in posts)


def test_normalizing_a_clean_thread_changes_nothing():
    body = "First post.\n\nSecond post."

    assert formats.normalize_thread(body) == body


def test_normalizing_is_idempotent():
    body = "1/ Hook here.\n\n2/ " + "z" * 350

    once = formats.normalize_thread(body)

    assert formats.normalize_thread(once) == once


def test_a_thread_is_capped_rather_than_published_at_any_length():
    body = "\n\n".join(f"Post number {i}." for i in range(40))

    posts = formats.parse_thread(formats.normalize_thread(body))

    assert len(posts) == formats.MAX_THREAD_POSTS


def test_a_thread_of_one_post_is_reported_as_a_problem():
    assert formats.thread_problems("Just the one.") == [
        f"A thread needs at least {formats.MIN_THREAD_POSTS} posts — separate "
        "them with a blank line."
    ]


def test_an_over_long_post_is_reported_with_its_number_and_length():
    body = "Fine.\n\n" + "q" * 300

    problems = formats.thread_problems(body)

    assert problems == ["Post 2 is 300 characters. The limit is 280."]


def test_a_good_thread_has_no_problems():
    assert formats.thread_problems("One idea.\n\nAnother idea.") == []


# --------------------------------------------------------------------------- #
# Changelogs                                                                   #
# --------------------------------------------------------------------------- #


def test_sections_and_their_bullets_are_parsed():
    body = "## Added\n\n- Templates\n- Threads\n\n## Fixed\n\n- A slug collision"

    assert formats.parse_changelog(body) == [
        ("Added", ["Templates", "Threads"]),
        ("Fixed", ["A slug collision"]),
    ]


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("New Features", "Added"),
        ("Bug Fixes", "Fixed"),
        ("Improvements", "Changed"),
        ("### Security", "Security"),
        ("Removals", "Removed"),
    ],
)
def test_a_models_synonym_lands_in_the_right_section(heading, expected):
    """'Bug Fixes' and 'Fixed' must not become two sections that read as two."""
    assert formats.canonical_section(heading.lstrip("# ")) == expected


def test_a_heading_that_means_nothing_is_not_a_section():
    assert formats.canonical_section("Notes from the team") is None


def test_prose_before_any_heading_is_dropped():
    """The padding the format exists to prevent."""
    body = "We're excited to share this release!\n\n## Fixed\n\n- The bug"

    assert formats.parse_changelog(body) == [("Fixed", ["The bug"])]


def test_an_unbulleted_line_under_a_real_section_is_kept():
    """Nearly always a bullet whose dash went missing, not prose."""
    body = "## Fixed\n\nThe bug\n- The other bug"

    assert formats.parse_changelog(body) == [("Fixed", ["The bug", "The other bug"])]


def test_duplicate_entries_are_collapsed():
    body = "## Fixed\n\n- The bug\n- The bug"

    assert formats.parse_changelog(body) == [("Fixed", ["The bug"])]


def test_rendering_uses_the_canonical_order_not_the_written_one():
    sections = [("Fixed", ["b"]), ("Added", ["a"])]

    assert formats.render_changelog(sections) == "## Added\n\n- a\n\n## Fixed\n\n- b"


def test_an_empty_section_is_not_rendered():
    assert formats.render_changelog([("Added", []), ("Fixed", ["b"])]) == "## Fixed\n\n- b"


def test_normalizing_a_changelog_is_idempotent():
    body = "## Bug Fixes\n\n- Something\n\n## New Features\n\n- Another"

    once = formats.normalize_changelog(body)

    assert formats.normalize_changelog(once) == once
    assert once.startswith("## Added")


def test_a_section_is_capped_because_a_git_log_is_not_a_changelog():
    body = "## Fixed\n\n" + "\n".join(f"- Fix number {i}" for i in range(40))

    entries = formats.parse_changelog(formats.normalize_changelog(body))[0][1]

    assert len(entries) == formats.MAX_ENTRIES_PER_SECTION


def test_an_empty_changelog_is_reported():
    assert formats.changelog_problems("") == [
        "A changelog needs at least one entry under a heading like ## Fixed."
    ]


def test_a_good_changelog_has_no_problems():
    assert formats.changelog_problems("## Added\n\n- A thing") == []


# --------------------------------------------------------------------------- #
# Changelogs from commit subjects — the fallback worth publishing              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("subject", "section", "text"),
    [
        ("feat: add RSS triggers", "Added", "add RSS triggers"),
        ("feat(triggers)!: add polling", "Added", "add polling"),
        ("fix: stop the crash", "Fixed", "stop the crash"),
        ("docs: tidy the readme", "Changed", "tidy the readme"),
        ("revert: undo that", "Removed", "undo that"),
        ("security: rotate the key", "Security", "rotate the key"),
    ],
)
def test_a_conventional_commit_classifies_itself(subject, section, text):
    assert formats.classify_change(subject) == (section, text)


@pytest.mark.parametrize(
    ("subject", "section"),
    [
        ("Add a settings page", "Added"),
        ("Fixed the parser", "Fixed"),
        ("Remove the old flag", "Removed"),
        ("Deprecate the v1 endpoint", "Deprecated"),
        ("Rework the internals", "Changed"),
    ],
)
def test_a_leading_verb_classifies_a_plain_commit(subject, section):
    """For the projects that do not write conventional commits."""
    assert formats.classify_change(subject)[0] == section


def test_an_unrecognisable_change_is_honestly_filed_under_changed():
    assert formats.classify_change("Friday")[0] == "Changed"


def test_a_changelog_is_assembled_from_commits_with_no_model_involved():
    body = formats.changelog_from_items(
        ["feat: add templates", "fix: slug collision", "chore: bump deps"]
    )

    assert body == (
        "## Added\n\n- Add templates\n\n"
        "## Changed\n\n- Bump deps\n\n"
        "## Fixed\n\n- Slug collision"
    )


def test_assembled_entries_are_sentence_cased():
    """Commit subjects are lowercase by convention; a changelog is not."""
    assert "- Add templates" in formats.changelog_from_items(["feat: add templates"])


def test_an_empty_commit_list_makes_an_empty_changelog():
    assert formats.changelog_from_items([]) == ""


def test_blank_items_do_not_become_empty_bullets():
    assert formats.changelog_from_items(["", "   ", "fix: real one"]) == (
        "## Fixed\n\n- Real one"
    )


# --------------------------------------------------------------------------- #
# The two entry points                                                         #
# --------------------------------------------------------------------------- #


def test_normalize_dispatches_on_the_content_type():
    assert formats.normalize("1/ a\n\n2/ b", ContentType.SOCIAL_THREAD) == "a\n\nb"
    assert formats.normalize("## Bug Fixes\n\n- x", ContentType.CHANGELOG) == (
        "## Fixed\n\n- x"
    )


def test_an_article_body_is_never_rewritten():
    body = "## Heading\n\nSome prose that a thread rule would mangle."

    assert formats.normalize(body, ContentType.TUTORIAL) == body
    assert formats.problems(body, ContentType.TUTORIAL) == []


# --------------------------------------------------------------------------- #
# Too thin to store                                                            #
# --------------------------------------------------------------------------- #


def test_a_short_article_is_too_thin():
    assert formats.too_thin_to_store("Three words here.", ContentType.TUTORIAL)


def test_a_long_enough_article_is_not():
    assert formats.too_thin_to_store("word " * 60, ContentType.TUTORIAL) is None


def test_a_complete_changelog_is_never_too_thin_for_being_short():
    """Six words, and a correct changelog. The whole reason this is not a word count."""
    body = "## Fixed\n\n- The parser crash"
    assert formats.too_thin_to_store(body, ContentType.CHANGELOG) is None


def test_a_changelog_with_nothing_in_it_is_too_thin():
    assert formats.too_thin_to_store("Thanks for reading!", ContentType.CHANGELOG)


def test_a_two_post_thread_is_enough_however_few_words():
    assert formats.too_thin_to_store("Ship it.\n\nHere is why.", ContentType.SOCIAL_THREAD) is None


def test_a_one_post_thread_is_not_a_thread():
    assert formats.too_thin_to_store("Just the one post.", ContentType.SOCIAL_THREAD)


# --------------------------------------------------------------------------- #
# The changelog panel has to be able to say what is wrong                      #
# --------------------------------------------------------------------------- #


def test_a_heading_that_names_no_section_is_reported():
    """`parse_changelog` canonicalises headings, so the branch looking for an
    unknown name in its output could never fire.

    An author writing `## Enhancements` got a clean panel while their entries
    were already being read as `Changed`.
    """
    body = "## Enhancements\n\n- Faster startup\n\n## Fixed\n\n- The parser crash"

    problems = formats.changelog_problems(body)

    assert len(problems) == 1
    assert "Enhancements" in problems[0]
    # And it says what is happening to the entries, not just that the name is wrong.
    assert "Changed" in problems[0]


def test_the_reported_heading_is_the_one_the_author_typed():
    body = "### Bits and bobs\n\n- A thing"
    assert "Bits and bobs" in formats.changelog_problems(body)[0]


def test_an_alias_heading_is_not_reported():
    """`Bug Fixes` means Fixed, and the panel should stay quiet about it."""
    assert formats.changelog_problems("## Bug Fixes\n\n- The parser crash") == []


def test_canonical_headings_are_not_reported():
    body = "## Added\n\n- RSS triggers\n\n## Fixed\n\n- The parser crash"
    assert formats.changelog_problems(body) == []


def test_each_unrecognised_heading_is_named_once():
    body = "## Enhancements\n\n- One\n\n## Enhancements\n\n- Two\n\n## Notes\n\n- Three"
    assert formats.unrecognised_headings(body) == ["Enhancements", "Notes"]
