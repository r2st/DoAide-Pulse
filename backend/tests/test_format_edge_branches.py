"""The arms of the thread splitter and the changelog classifier nothing walked.

Each of these is a branch that only runs on input a well-behaved model does not
produce — an empty tail after a full stop, a commit type nobody has taught the
table about, the same change listed twice. They are the arms that decide whether
a bad input degrades or throws.
"""
from __future__ import annotations

from app.services import formats

# --------------------------------------------------------------------------- #
# Splitting                                                                    #
# --------------------------------------------------------------------------- #


def test_a_trailing_full_stop_does_not_produce_an_empty_post():
    """Text ending in punctuation plus whitespace must not gain a blank post.

    ``split_post`` strips before splitting, so the trailing space is gone
    before ``_SENTENCE_END`` ever sees it — that is *why* the tail cannot be
    empty, not a guard that catches it afterwards.
    """
    text = "Alpha beta. Gamma delta. Epsilon zeta. "

    posts = formats.split_post(text, limit=20)

    assert posts == ["Alpha beta.", "Gamma delta.", "Epsilon zeta."]
    assert all(post.strip() for post in posts)


def test_the_tokeniser_contract_split_post_relies_on():
    """Every token non-empty, none over the limit, at least one for real text.

    ``split_post`` appends ``current`` without checking it, at the flush and at
    the tail. That is only correct while this holds: an empty token would
    become a blank post, and an over-long one would arrive when nothing had
    accumulated yet and be appended as an empty string. The guards that used to
    stand there could never fire, so they hid a broken tokeniser rather than
    reporting one. This is the assertion that replaces them.
    """
    corpus = [
        "Alpha beta. Gamma delta. Epsilon zeta. ",
        "Wait.. What now?",
        "First one.... Second one." + " tail" * 60,
        "https://example.com/" + "x" * 400 + " and then some trailing words.",
        "A" * 40 + ". " + "B" * 40 + ".",
        "!?!. . . ?!",
        "\n\nragged\t\twhitespace\n everywhere.  \n",
        "no punctuation at all just words that run on and on and on",
        "。".join(["multibyte"] * 30),
        "x" * 400,
    ]
    for text in corpus:
        for limit in (1, 2, 7, 20, 45, 100, 280):
            stripped = text.strip()
            tokens = formats._tokens(stripped, limit)

            assert all(tokens), f"empty token from {text!r} at limit {limit}"
            assert all(len(t) <= limit for t in tokens), f"{text!r} at limit {limit}"
            if len(stripped) > limit:
                assert tokens, f"no tokens from {text!r} at limit {limit}"

            posts = formats.split_post(text, limit=limit)
            assert all(posts), f"blank post from {text!r} at limit {limit}"
            assert all(len(p) <= limit for p in posts), f"{text!r} at limit {limit}"


def test_a_word_longer_than_the_limit_is_cut_rather_than_dropped():
    """A 90-character URL in a 40-character post has no good answer; losing it
    silently is the worst one."""
    url = "https://example.com/" + "a" * 80
    text = f"See {url} for more."

    posts = formats.split_post(text, limit=40)

    # Nothing but the separating whitespace is lost, and every piece fits.
    assert "".join(post.replace(" ", "") for post in posts) == text.replace(" ", "")
    assert all(len(post) <= 40 for post in posts)


def test_text_inside_the_limit_comes_back_whole():
    assert formats.split_post("Short enough.", limit=280) == ["Short enough."]


def test_empty_text_produces_no_posts():
    assert formats.split_post("   ", limit=280) == []


# --------------------------------------------------------------------------- #
# Reporting problems                                                           #
# --------------------------------------------------------------------------- #


def test_a_thread_past_the_publish_ceiling_says_the_tail_will_be_dropped():
    """``normalize_thread`` truncates silently; the editor has to say so."""
    body = "\n\n".join(f"Post number {i}." for i in range(formats.MAX_THREAD_POSTS + 3))

    problems = formats.thread_problems(body)

    assert any(str(formats.MAX_THREAD_POSTS) in p and "dropped" in p for p in problems)


def test_a_thread_at_the_ceiling_exactly_is_not_complained_about():
    body = "\n\n".join(f"Post number {i}." for i in range(formats.MAX_THREAD_POSTS))

    assert formats.thread_problems(body) == []


# --------------------------------------------------------------------------- #
# Classifying changes                                                          #
# --------------------------------------------------------------------------- #


def test_an_unknown_conventional_type_keeps_its_prefix_and_falls_through():
    """``wip:`` matches the conventional-commit shape but means nothing here.

    Stripping the prefix would be a lie about a type Herald does not understand,
    so the whole subject is kept and the section falls back to ``Changed``.
    """
    section, subject = formats.classify_change("wip: half of the new parser")

    assert section == "Changed"
    assert subject == "wip: half of the new parser"


def test_a_known_conventional_type_loses_its_prefix():
    section, subject = formats.classify_change("fix: the off-by-one in the cursor")

    assert section == "Fixed"
    assert subject == "the off-by-one in the cursor"


def test_the_same_change_listed_twice_appears_once():
    """Cherry-picks and merge commits produce genuine duplicates."""
    changelog = formats.changelog_from_items(
        [
            "fix: the off-by-one in the cursor",
            "fix: the off-by-one in the cursor",
            "feat: a second thing",
        ]
    )

    assert changelog.count("The off-by-one in the cursor") == 1
    assert "A second thing" in changelog


def test_a_bare_bullet_with_nothing_after_it_is_dropped():
    assert formats.changelog_from_items(["-", "  ", "fix: something real"]).count("\n- ") == 1


def test_no_items_at_all_renders_nothing():
    assert formats.changelog_from_items([]) == ""
