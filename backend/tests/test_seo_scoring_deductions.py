"""The SEO deductions and JSON-LD fields nothing was exercising.

``seo_score`` gates the autopilot: a deduction that never fires is a piece that
auto-publishes when it should have gone to review. Each test here moves exactly
one thing and asserts the score moved with it, so a reweighting shows up as a
failure rather than as a silently different gate.
"""
from __future__ import annotations

import json

from app.services import seo

GOOD_BODY = "\n\n".join(
    [
        "# Herald ships scheduling",
        "Herald scheduling now picks a slot for every platform you have "
        "connected, and this opening paragraph is long enough to count as a "
        "real one.",
        "## What scheduling changes",
        " ".join(["word"] * 380)
        + " scheduling is the point here, and scheduling again.",
    ]
)

GOOD = {
    "title": "Herald ships scheduling",
    "body_markdown": GOOD_BODY,
    "meta_description": "Herald scheduling now picks a slot on every platform "
    "you have connected, one per platform.",
    "keywords": ["scheduling"],
    "cover_image_url": "https://example.com/cover.png",
    "focus_keyword": "scheduling",
    "slug": "herald-ships-scheduling",
}


def _score(**over) -> int:
    return seo.seo_score(**{**GOOD, **over})


def test_a_clean_piece_scores_at_the_top():
    assert _score() == 100


def test_a_title_past_the_search_result_cut_off_costs_five_points():
    long_title = "Herald ships scheduling " + "and more things besides " * 5

    assert len(long_title) > seo.TITLE_MAX
    assert _score(title=long_title) == 95


def test_the_same_long_title_is_reported_as_a_warning_with_its_length():
    long_title = "Herald ships scheduling " + "and more things besides " * 5

    issues = seo.audit(**{**GOOD, "title": long_title})

    title_issues = [i for i in issues if i.field == "title"]
    assert title_issues and title_issues[0].level == "warn"
    assert str(len(long_title)) in title_issues[0].message


def test_skipping_a_heading_level_costs_five_points():
    """H1 straight to H4 reads as a broken outline to a crawler."""
    skipped = GOOD_BODY.replace("## What scheduling", "#### What scheduling")

    assert _score(body_markdown=skipped) == 95


def test_an_image_with_no_alt_text_costs_five_points():
    with_image = GOOD_BODY + "\n\n![](https://example.com/diagram.png)"

    assert _score(body_markdown=with_image) == 95


def test_an_image_that_has_alt_text_costs_nothing():
    with_image = GOOD_BODY + "\n\n![A diagram](https://example.com/diagram.png)"

    assert _score(body_markdown=with_image) == 100


def test_a_slug_too_long_for_a_search_result_costs_three_points():
    long_slug = "herald-ships-scheduling-" + "and-more-besides-" * 4

    assert len(long_slug) > 60
    assert _score(slug=long_slug + "scheduling") == 97


def test_a_focus_keyword_that_is_only_whitespace_matches_nothing():
    """``"  ".split()`` is empty — the pattern would match every position."""
    assert seo._keyword_occurrences("anything at all", "   ") == 0
    assert seo._contains_keyword("anything at all", "  ") is False


# --------------------------------------------------------------------------- #
# JSON-LD                                                                     #
# --------------------------------------------------------------------------- #


def test_an_edited_post_carries_its_modified_date():
    raw = seo.build_json_ld(
        title="Herald ships scheduling",
        body_markdown=GOOD_BODY,
        meta_description="Scheduling, now built in.",
        url="https://example.com/scheduling",
        published_at="2026-07-01T09:00:00+00:00",
        modified_at="2026-07-20T09:00:00+00:00",
    )

    assert json.loads(raw)["dateModified"] == "2026-07-20T09:00:00+00:00"


def test_an_unedited_post_carries_no_modified_date():
    raw = seo.build_json_ld(
        title="Herald ships scheduling",
        body_markdown=GOOD_BODY,
        meta_description="Scheduling, now built in.",
        url="https://example.com/scheduling",
        published_at="2026-07-01T09:00:00+00:00",
    )

    assert "dateModified" not in json.loads(raw)


def test_a_publisher_logo_is_nested_under_the_publisher_not_the_article():
    raw = seo.build_json_ld(
        title="Herald ships scheduling",
        body_markdown=GOOD_BODY,
        meta_description="Scheduling, now built in.",
        url="https://example.com/scheduling",
        publisher_name="Herald",
        publisher_logo_url="https://example.com/logo.png",
    )

    schema = json.loads(raw)
    assert schema["publisher"] == {
        "@type": "Organization",
        "name": "Herald",
        "logo": {"@type": "ImageObject", "url": "https://example.com/logo.png"},
    }


def test_a_logo_with_no_publisher_to_hang_it_on_is_dropped():
    raw = seo.build_json_ld(
        title="Herald ships scheduling",
        body_markdown=GOOD_BODY,
        meta_description="Scheduling, now built in.",
        url="https://example.com/scheduling",
        publisher_logo_url="https://example.com/logo.png",
    )

    assert "publisher" not in json.loads(raw)
