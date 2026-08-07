"""What the unfurled card will look like, and what breaks it.

The service is pure — no database, no network — so these are plain function
tests. Three things they exist to pin down:

* **Clipping is per network and non-destructive.** The tags carry the real
  title; only the previews are cut, and cutting happens on a word boundary.
* **A relative image is not an image.** Crawlers fetch from their own servers,
  so a relative cover is a blank card and must be reported as an error rather
  than shown in the preview.
* **The description falls back the way a crawler falls back**, so the panel
  shows what will actually be rendered rather than what we wish were set.
"""
from __future__ import annotations

import pytest

from app.services import social_cards

#: Long enough to clip on *every* network, including LinkedIn's generous 119 —
#: otherwise the per-network assertions below pass for the wrong reason.
LONG_TITLE = (
    "How we cut our continuous integration pipeline from forty minutes down to "
    "under four using aggressive caching, a better runner, and far fewer steps"
)


# --------------------------------------------------------------------------- #
# clip                                                                         #
# --------------------------------------------------------------------------- #


def test_a_title_within_the_limit_is_untouched():
    assert social_cards.clip("Short enough", 70) == "Short enough"


def test_a_title_at_exactly_the_limit_keeps_every_character():
    text = "x" * 70
    assert social_cards.clip(text, 70) == text


def test_clipping_falls_back_to_a_word_boundary():
    clipped = social_cards.clip(LONG_TITLE, 40)

    assert clipped.endswith("…")
    assert len(clipped) <= 40
    # The defining property: no half-words. Everything before the ellipsis is a
    # word the author actually wrote.
    assert LONG_TITLE.startswith(clipped[:-1].rstrip())
    assert not clipped[:-1].endswith(" ")


def test_a_single_word_longer_than_the_budget_is_cut_mid_word():
    # There is no boundary to fall back to, and returning nothing would be
    # worse than returning a prefix.
    clipped = social_cards.clip("supercalifragilistic", 10)

    assert clipped == "supercali…"


def test_markdown_and_newlines_are_flattened_before_clipping():
    # A card has no line breaks and shows asterisks literally.
    assert social_cards.clip("**Bold**\ntitle", 70) == "Bold title"


# --------------------------------------------------------------------------- #
# domain_of                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/blog/post", "example.com"),
        # www. is dropped because the networks drop it.
        ("https://www.example.com/post", "example.com"),
        ("https://Blog.Example.COM/post", "blog.example.com"),
        ("", ""),
        ("not-a-url", ""),
    ],
)
def test_domain_of(url, expected):
    assert social_cards.domain_of(url) == expected


# --------------------------------------------------------------------------- #
# previews                                                                     #
# --------------------------------------------------------------------------- #


def test_every_network_gets_a_preview_in_a_stable_order():
    result = social_cards.previews(title="Hello", url="https://example.com/p")

    assert [p.network for p in result] == list(social_cards.NETWORKS)


def test_a_long_title_is_clipped_per_network_and_flagged():
    result = {
        p.network: p
        for p in social_cards.previews(title=LONG_TITLE, url="https://example.com/p")
    }

    # X clips hardest, LinkedIn is the most generous — so X's is strictly
    # shorter, and both are marked as clipped.
    assert len(result["x"].title) < len(result["linkedin"].title)
    assert result["x"].title_clipped
    assert result["linkedin"].title_clipped


def test_a_short_title_is_not_flagged_anywhere():
    result = social_cards.previews(title="A short title", url="https://example.com/p")

    assert not any(p.title_clipped for p in result)
    assert all(p.title == "A short title" for p in result)


def test_an_image_selects_the_large_card_layout():
    result = social_cards.previews(
        title="Hello",
        url="https://example.com/p",
        cover_image_url="https://cdn.example.com/cover.png",
    )

    assert all(p.card_type == "summary_large_image" for p in result)
    assert all(p.image_url == "https://cdn.example.com/cover.png" for p in result)


def test_no_image_falls_back_to_the_small_card():
    result = social_cards.previews(title="Hello", url="https://example.com/p")

    assert all(p.card_type == "summary" for p in result)
    assert all(p.image_url is None for p in result)


def test_a_relative_cover_is_not_shown_as_an_image():
    # The crawler cannot resolve it, so promising it in the preview would be a
    # lie — it degrades to the text-only card exactly as it will in the feed.
    result = social_cards.previews(
        title="Hello", url="https://example.com/p", cover_image_url="/img/cover.png"
    )

    assert all(p.image_url is None for p in result)
    assert all(p.card_type == "summary" for p in result)


def test_the_description_falls_back_from_meta_to_excerpt_to_body():
    body = "# Heading\n\nThe first real paragraph of the post, long enough to count."

    with_meta = social_cards.previews(
        title="T", meta_description="The meta.", excerpt="The excerpt.", body_markdown=body
    )
    with_excerpt = social_cards.previews(
        title="T", meta_description="", excerpt="The excerpt.", body_markdown=body
    )
    with_body = social_cards.previews(
        title="T", meta_description="", excerpt="", body_markdown=body
    )

    assert with_meta[0].description == "The meta."
    assert with_excerpt[0].description == "The excerpt."
    # Falls through to the body rather than rendering an empty line.
    assert "first real paragraph" in with_body[0].description


# --------------------------------------------------------------------------- #
# meta_tags                                                                    #
# --------------------------------------------------------------------------- #


def test_the_tags_carry_the_full_title_not_the_clipped_one():
    tags = dict(social_cards.meta_tags(title=LONG_TITLE, url="https://example.com/p"))

    # Clipping in the tag would mean every network showed the shortest
    # network's version.
    assert tags["og:title"] == LONG_TITLE
    assert tags["twitter:title"] == LONG_TITLE


def test_an_image_produces_the_large_card_tag_and_dimensions():
    tags = dict(
        social_cards.meta_tags(
            title="T", url="https://example.com/p", cover_image_url="https://cdn.example.com/c.png"
        )
    )

    assert tags["twitter:card"] == "summary_large_image"
    assert tags["og:image"] == "https://cdn.example.com/c.png"
    assert tags["og:image:width"] == str(social_cards.RECOMMENDED_IMAGE[0])
    assert tags["og:image:height"] == str(social_cards.RECOMMENDED_IMAGE[1])


def test_no_image_downgrades_the_card_type_and_emits_no_image_tags():
    tags = social_cards.meta_tags(title="T", url="https://example.com/p")
    keys = [key for key, _ in tags]

    assert dict(tags)["twitter:card"] == "summary"
    assert "og:image" not in keys
    # An og:image:width with no og:image is the kind of thing validators flag.
    assert "og:image:width" not in keys


def test_every_keyword_becomes_its_own_article_tag():
    tags = social_cards.meta_tags(
        title="T", url="https://example.com/p", tags=["python", "fastapi", "testing"]
    )
    emitted = [value for key, value in tags if key == "article:tag"]

    # A dict would have kept only the last one — the reason meta_tags returns
    # a list of pairs.
    assert emitted == ["python", "fastapi", "testing"]


def test_a_handle_without_an_at_sign_gets_one():
    tags = dict(social_cards.meta_tags(title="T", url="https://e.com/p", author_handle="herald"))

    assert tags["twitter:creator"] == "@herald"


def test_a_handle_that_already_has_an_at_sign_is_not_doubled():
    tags = dict(social_cards.meta_tags(title="T", url="https://e.com/p", author_handle="@herald"))

    assert tags["twitter:creator"] == "@herald"


# --------------------------------------------------------------------------- #
# render_meta_tags                                                             #
# --------------------------------------------------------------------------- #


def test_open_graph_uses_property_and_twitter_uses_name():
    html = social_cards.render_meta_tags(
        [("og:title", "Hello"), ("twitter:title", "Hello")]
    )

    assert '<meta property="og:title" content="Hello">' in html
    assert '<meta name="twitter:title" content="Hello">' in html


def test_a_quote_in_the_title_cannot_break_out_of_the_attribute():
    html = social_cards.render_meta_tags(
        [("og:title", 'He said "hi" <script>alert(1)</script>')]
    )

    assert "<script>" not in html
    assert '"hi"' not in html
    assert "&quot;" in html and "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# front_matter_keys                                                            #
# --------------------------------------------------------------------------- #


def test_front_matter_keys_are_camel_cased_for_static_site_themes():
    tags = social_cards.meta_tags(
        title="Hello", url="https://e.com/p", cover_image_url="https://cdn.e.com/c.png"
    )

    keys = social_cards.front_matter_keys(tags)

    assert keys["ogTitle"] == "Hello"
    assert keys["ogImage"] == "https://cdn.e.com/c.png"
    assert keys["twitterCard"] == "summary_large_image"


def test_front_matter_omits_keys_with_no_value():
    tags = social_cards.meta_tags(title="Hello", url="https://e.com/p")

    keys = social_cards.front_matter_keys(tags)

    # No cover image was set, so no ogImage key — writing an empty one would
    # make a theme render an empty <meta>.
    assert "ogImage" not in keys
    assert keys["twitterCard"] == "summary"


# --------------------------------------------------------------------------- #
# audit                                                                        #
# --------------------------------------------------------------------------- #


def _fields(issues, level=None):
    return {i.field for i in issues if level is None or i.level == level}


def test_a_missing_cover_image_is_an_error():
    issues = social_cards.audit(title="A fine title", meta_description="A" * 80)

    assert "cover_image_url" in _fields(issues, "error")


def test_a_relative_cover_image_is_an_error():
    issues = social_cards.audit(
        title="A fine title", meta_description="A" * 80, cover_image_url="/img/c.png"
    )

    errors = [i for i in issues if i.field == "cover_image_url" and i.level == "error"]
    assert errors and "absolute" in errors[0].message


def test_an_svg_cover_is_a_warning_not_an_error():
    # It is a real image and it will load in a browser; it just will not unfurl.
    issues = social_cards.audit(
        title="A fine title",
        meta_description="A" * 80,
        cover_image_url="https://cdn.example.com/cover.svg",
    )

    assert "cover_image_url" in _fields(issues, "warn")
    assert "cover_image_url" not in _fields(issues, "error")


def test_a_query_string_does_not_hide_the_extension():
    issues = social_cards.audit(
        title="A fine title",
        meta_description="A" * 80,
        cover_image_url="https://cdn.example.com/cover.svg?w=1200",
    )

    assert "cover_image_url" in _fields(issues, "warn")


def test_a_good_png_cover_raises_nothing_about_the_image():
    issues = social_cards.audit(
        title="A perfectly reasonable title",
        meta_description="A" * 80,
        cover_image_url="https://cdn.example.com/cover.png",
    )

    assert "cover_image_url" not in _fields(issues)


def test_a_short_description_is_flagged():
    issues = social_cards.audit(
        title="A fine title",
        meta_description="Too short.",
        cover_image_url="https://cdn.example.com/c.png",
    )

    assert "meta_description" in _fields(issues, "warn")


def test_a_long_title_is_reported_once_not_once_per_network():
    issues = social_cards.audit(
        title=LONG_TITLE,
        meta_description="A" * 80,
        cover_image_url="https://cdn.example.com/c.png",
    )

    title_issues = [i for i in issues if i.field == "title"]
    assert len(title_issues) == 1


def test_errors_are_sorted_before_warnings():
    issues = social_cards.audit(title=LONG_TITLE, meta_description="Short.")

    levels = [i.level for i in issues]
    assert levels == sorted(levels, key=lambda level: {"error": 0, "warn": 1}[level])


def test_a_complete_post_has_no_issues():
    issues = social_cards.audit(
        title="A clean, well-sized headline",
        meta_description=(
            "A description that comfortably clears the fifty-character floor "
            "and reads like a real sentence."
        ),
        cover_image_url="https://cdn.example.com/cover.png",
    )

    assert issues == []


# --------------------------------------------------------------------------- #
# summary                                                                      #
# --------------------------------------------------------------------------- #


def test_summary_bundles_everything_the_panel_needs():
    result = social_cards.summary(
        title="A clean headline",
        url="https://example.com/blog/post",
        meta_description="A" * 80,
        cover_image_url="https://cdn.example.com/cover.png",
        site_name="Herald",
        tags=["python"],
    )

    assert len(result["previews"]) == len(social_cards.NETWORKS)
    assert result["issues"] == []
    assert result["meta_html"].startswith("<meta ")
    assert {"key": "og:site_name", "value": "Herald"} in result["meta_tags"]
    assert result["recommended_image"] == {"width": 1200, "height": 630}


# --------------------------------------------------------------------------- #
# The tags and the preview have to agree about the image                       #
# --------------------------------------------------------------------------- #


def test_a_relative_cover_produces_no_image_tags_and_the_small_card():
    """The preview refused a relative path; the tags emitted it anyway.

    That pairing — `summary_large_image` plus an `og:image` no crawler can
    resolve — is the grey rectangle this module exists to prevent, and it is
    the one Herald published: `GitAdapter.build_file` writes these keys into a
    real page's front matter.
    """
    tags = dict(
        social_cards.meta_tags(
            title="A post",
            url="https://example.com/blog/post",
            meta_description="A" * 80,
            cover_image_url="/images/cover.png",
        )
    )

    assert tags["twitter:card"] == "summary"
    assert "og:image" not in tags
    assert "twitter:image" not in tags


def test_an_absolute_cover_still_gets_the_large_card():
    tags = dict(
        social_cards.meta_tags(
            title="A post",
            url="https://example.com/blog/post",
            meta_description="A" * 80,
            cover_image_url="https://cdn.example.com/cover.png",
        )
    )

    assert tags["twitter:card"] == "summary_large_image"
    assert tags["og:image"] == "https://cdn.example.com/cover.png"
    assert tags["twitter:image"] == "https://cdn.example.com/cover.png"


def test_the_git_front_matter_never_claims_an_image_it_cannot_resolve():
    """What actually lands on disk, for the destination Herald controls."""
    keys = social_cards.front_matter_keys(
        social_cards.meta_tags(
            title="A post",
            url="https://example.com/blog/post",
            meta_description="A" * 80,
            cover_image_url="images/cover.png",
        )
    )

    assert "ogImage" not in keys
    assert keys["twitterCard"] == "summary"


@pytest.mark.parametrize(
    "cover", ["/images/cover.png", "images/cover.png", "  ", None, "ftp://x/c.png"]
)
def test_the_card_type_is_the_same_in_the_tags_and_in_every_preview(cover):
    """The invariant, not the one example: one decision, read in two places."""
    tags = dict(
        social_cards.meta_tags(
            title="A post", url="https://example.com/p", cover_image_url=cover
        )
    )
    shown = social_cards.previews(
        title="A post", url="https://example.com/p", cover_image_url=cover
    )

    assert {p.card_type for p in shown} == {tags["twitter:card"]}
    assert {p.image_url for p in shown} == {tags.get("og:image")}
