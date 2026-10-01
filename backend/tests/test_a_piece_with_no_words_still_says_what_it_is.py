"""A body that is all images flattens to nothing, and LinkedIn posted the nothing.

Pulse stores one body in Markdown and adapts it per platform. Every adapter
that needs plain text gets there through
:func:`app.services.publishers.formatting.to_plain_text`, which renders the
Markdown and takes the text out of the result — and an ``<img>`` has no text in
it. So a piece whose body is images and nothing else, which is what a changelog
assembled from screenshots or a release note built around three diagrams looks
like, flattens to the empty string.

Two of the three short-form adapters already had an answer. Mastodon and Bluesky
both compose from ``request.excerpt or request.title``, and a piece always has a
title: ``ContentCreate`` requires one, and ``content_generator._assemble``
substitutes ``"{project}: {label}"`` when the model does not supply one. So the
worst case there is a post that leads with the headline.

LinkedIn composed from ``request.excerpt or to_plain_text(body)``, and that
second term is the one that can come back empty. With no excerpt either — which
is the ordinary state of a hand-written draft, since ``excerpt`` defaults to
``""`` on ``ContentCreate`` — the commentary was the empty string, and
``truncate_for_linkedin`` then joined it to the link with the separator it uses
between prose and a link. What went out under the user's name was two blank
lines and a bare URL: no title, no words, and nothing saying what it pointed at.

Both halves are fixed and both are pinned here, because either one alone leaves
the other reachable — the title fallback is what makes the post say something,
and the separator rule is what stops any *other* caller reaching the same shape.
"""
from __future__ import annotations

import pytest

from app.services.publishers import formatting
from app.services.publishers.base import PublishRequest
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.mastodon import MastodonAdapter

#: What the editor produces for a piece carrying diagrams and no prose.
IMAGES_ONLY = (
    "![The publish pipeline, end to end](https://cdn.example.com/pipeline.png)\n\n"
    "![Retry backoff over four attempts](https://cdn.example.com/backoff.png)\n"
)


def _request(**overrides) -> PublishRequest:
    fields = {
        "title": "How Pulse retries a failed publish",
        "body_markdown": IMAGES_ONLY,
        "excerpt": "",
        "meta_description": "",
        "tags": ["python", "devtools"],
        "canonical_url": "https://herald.example.com/blog/retries",
        "project_url": "https://herald.example.com",
        "project_name": "Pulse",
    }
    fields.update(overrides)
    return PublishRequest(**fields)


# --------------------------------------------------------------------------- #
# The premise                                                                  #
# --------------------------------------------------------------------------- #


def test_a_body_of_images_flattens_to_nothing():
    """Not a bug in ``to_plain_text`` — there is genuinely no text in it."""
    assert formatting.to_plain_text(IMAGES_ONLY) == ""
    # And the images are still there in the rendered HTML, which is what the
    # platforms with a body field actually receive.
    assert formatting.to_html(IMAGES_ONLY).count("<img") == 2


# --------------------------------------------------------------------------- #
# LinkedIn                                                                     #
# --------------------------------------------------------------------------- #


def test_linkedin_leads_with_the_title_when_there_is_no_text():
    commentary = LinkedInAdapter().build_commentary(_request())
    assert commentary.startswith("How Pulse retries a failed publish")


def test_linkedin_does_not_open_with_blank_lines():
    """The shape that actually went out: "\\n\\n<url>", and nothing else."""
    commentary = LinkedInAdapter().build_commentary(_request())
    assert not commentary.startswith("\n")
    assert commentary.strip() == commentary


def test_linkedin_still_carries_the_link_and_the_hashtags():
    commentary = LinkedInAdapter().build_commentary(_request())
    assert "https://herald.example.com/blog/retries" in commentary
    assert "#python" in commentary


def test_linkedin_still_prefers_the_excerpt_when_there_is_one():
    """The fallback is a last resort, not a new first choice."""
    commentary = LinkedInAdapter().build_commentary(
        _request(excerpt="Backoff, budgets, and when Pulse gives up.")
    )
    assert commentary.startswith("Backoff, budgets, and when Pulse gives up.")


def test_linkedin_still_prefers_the_body_over_the_title():
    commentary = LinkedInAdapter().build_commentary(
        _request(body_markdown="## Retries\n\nPulse parks a failed publish.")
    )
    assert commentary.startswith("Retries")


@pytest.mark.parametrize("body", ["", "   ", "\n\n \t\n", "<!-- a comment -->"])
def test_every_way_of_writing_an_empty_body_still_says_something(body):
    """Whitespace, nothing at all, and markup that renders to nothing."""
    commentary = LinkedInAdapter().build_commentary(_request(body_markdown=body))
    assert commentary.startswith("How Pulse retries a failed publish")


# --------------------------------------------------------------------------- #
# truncate_for_linkedin, one layer down                                        #
# --------------------------------------------------------------------------- #


def test_an_empty_post_with_a_link_is_the_bare_link():
    assert formatting.truncate_for_linkedin("", url="https://example.com/a") == (
        "https://example.com/a"
    )


def test_a_whitespace_post_with_a_link_is_the_bare_link():
    assert formatting.truncate_for_linkedin("  \n ", url="https://example.com/a") == (
        "https://example.com/a"
    )


def test_this_matches_what_the_short_platforms_already_did():
    """``truncate_with_link`` has always answered the bare link here.

    Two functions disagreeing about the same empty string is how one of them
    ends up being the only one anybody tests.
    """
    empty = formatting.truncate_with_link("", url="https://example.com/a", limit=300)
    assert empty == formatting.truncate_for_linkedin("", url="https://example.com/a")


def test_an_empty_post_with_no_link_is_still_empty():
    assert formatting.truncate_for_linkedin("") == ""


def test_prose_and_a_link_are_still_separated():
    post = formatting.truncate_for_linkedin("A hook.", url="https://example.com/a")
    assert post == "A hook.\n\nhttps://example.com/a"


# --------------------------------------------------------------------------- #
# The two that already had an answer                                           #
# --------------------------------------------------------------------------- #


def test_mastodon_leads_with_the_title_when_there_is_no_text():
    status = MastodonAdapter().build_status(_request())
    assert status.startswith("How Pulse retries a failed publish")
    assert len(status) <= formatting.MASTODON_LIMIT


def test_bluesky_leads_with_the_title_when_there_is_no_text():
    text = BlueskyAdapter().build_text(_request())
    assert text.startswith("How Pulse retries a failed publish")
    assert len(text) <= formatting.BLUESKY_LIMIT


def test_no_short_form_adapter_produces_a_post_with_no_words():
    """The property all three now share, stated once as a property."""
    request = _request()
    posts = [
        LinkedInAdapter().build_commentary(request),
        MastodonAdapter().build_status(request),
        BlueskyAdapter().build_text(request),
    ]
    for post in posts:
        # Something that is not the link and not a hashtag.
        words = [
            word
            for word in post.split()
            if not word.startswith(("http", "#"))
        ]
        assert words, post
