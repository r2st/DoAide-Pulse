"""Tests for the SEO preflight gate and scoring logic."""
from __future__ import annotations

import pytest

from app.services.seo import seo_score


class TestSeoScore:
    """Verify the scoring function responds to each dimension."""

    def test_perfect_piece_scores_high(self):
        score = seo_score(
            title="AI Content Automation Platform for Developers",
            body_markdown=(
                "## AI Content Automation\n\n"
                "AI content automation is transforming how developers create "
                "documentation. AI content automation tools save hours of writing "
                "by generating accurate technical posts from repository activity.\n\n"
                "## How AI Content Automation Works\n\n"
                "When a developer pushes code, AI content automation picks up the "
                "changes and drafts a post. The process is fully automated.\n\n"
                + ("This is filler prose to reach the word count. " * 20)
            ),
            meta_description=(
                "AI content automation helps developers create technical posts "
                "from repository activity. Save hours of writing with automated drafts."
            ),
            keywords=["ai content automation", "developer tools", "technical writing"],
            focus_keyword="ai content automation",
            cover_image_url="https://example.com/cover.png",
            slug="ai-content-automation-platform",
        )
        assert score >= 70

    def test_empty_title_costs_points(self):
        score = seo_score(
            title="",
            body_markdown="Some body " * 100,
            meta_description="A description of moderate length for the test.",
            keywords=["testing"],
        )
        assert score < 90

    def test_missing_meta_description_costs_points(self):
        score = seo_score(
            title="A Good Title Here",
            body_markdown="Some body " * 100,
            meta_description="",
            keywords=["testing"],
        )
        assert score < 90

    def test_no_keywords_costs_points(self):
        score = seo_score(
            title="A Good Title Here",
            body_markdown="Some body " * 100,
            meta_description="A description of moderate length for the test.",
            keywords=[],
        )
        assert score < 100

    def test_no_headings_costs_points(self):
        score = seo_score(
            title="A Good Title Here",
            body_markdown="No headings just paragraphs. " * 50,
            meta_description="A description of moderate length for the test.",
            keywords=["testing"],
        )
        assert score < 95

    def test_short_body_costs_points(self):
        score = seo_score(
            title="A Good Title Here",
            body_markdown="Too short.",
            meta_description="A description of moderate length for the test.",
            keywords=["testing"],
        )
        assert score < 100


class TestProductSpotlightType:
    """Verify the new content type is wired correctly."""

    def test_product_spotlight_in_content_type_enum(self):
        from app.models.content import ContentType

        assert ContentType.PRODUCT_SPOTLIGHT.value == "product_spotlight"

    def test_product_spotlight_has_target_words(self):
        from app.models.content import TARGET_WORDS, ContentType

        assert ContentType.PRODUCT_SPOTLIGHT in TARGET_WORDS
        assert TARGET_WORDS[ContentType.PRODUCT_SPOTLIGHT] == 1000

    def test_product_spotlight_has_type_guidance(self):
        from app.services.content_generator import _TYPE_GUIDANCE
        from app.models.content import ContentType

        assert ContentType.PRODUCT_SPOTLIGHT in _TYPE_GUIDANCE
        assert "deep-dive" in _TYPE_GUIDANCE[ContentType.PRODUCT_SPOTLIGHT]

    def test_product_spotlight_is_article_format(self):
        from app.services.formats import format_of, ContentFormat

        assert format_of("product_spotlight") == ContentFormat.ARTICLE
