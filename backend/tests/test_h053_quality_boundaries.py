"""Boundary conditions in the quality scoring that the gate tests do not reach.

The existing ``test_a_piece_is_measured_before_it_asks_for_a_reader`` covers the
happy path and the obvious cases. This file exercises the boundary arithmetic:
what happens at the exact edges of the reading-ease band, the code-ratio
ceiling, and the weight redistribution for short bodies.
"""
from __future__ import annotations

import pytest

from app.services import quality


class TestReadabilityPoints:
    """The reading-ease component: 0–100, with an asymmetric band."""

    def test_exactly_at_the_lower_bound_is_full_marks(self):
        assert quality.readability_points(quality.READING_EASE_TARGET_MIN) == 100

    def test_exactly_at_the_upper_bound_is_full_marks(self):
        assert quality.readability_points(quality.READING_EASE_TARGET_MAX) == 100

    def test_mid_band_is_full_marks(self):
        mid = (quality.READING_EASE_TARGET_MIN + quality.READING_EASE_TARGET_MAX) / 2
        assert quality.readability_points(mid) == 100

    def test_one_point_below_the_band_loses_two_points(self):
        assert quality.readability_points(quality.READING_EASE_TARGET_MIN - 1) == 98

    def test_one_point_above_the_band_loses_one_point(self):
        assert quality.readability_points(quality.READING_EASE_TARGET_MAX + 1) == 99

    def test_far_below_the_band_floors_at_zero(self):
        assert quality.readability_points(0.0) == max(
            0, round(100 - 2 * quality.READING_EASE_TARGET_MIN)
        )

    def test_far_above_the_band_floors_at_zero(self):
        assert quality.readability_points(200.0) == 0

    def test_none_returns_none(self):
        assert quality.readability_points(None) is None


class TestCodePoints:
    """The code-density component: 0–100, one-sided."""

    def test_no_code_is_full_marks(self):
        assert quality.code_points(0.0) == 100

    def test_exactly_at_the_ideal_max_is_full_marks(self):
        assert quality.code_points(quality.CODE_RATIO_IDEAL_MAX) == 100

    def test_all_code_is_zero(self):
        assert quality.code_points(1.0) == 0

    def test_halfway_over_the_ceiling_is_halfway_down(self):
        midpoint = quality.CODE_RATIO_IDEAL_MAX + (1.0 - quality.CODE_RATIO_IDEAL_MAX) / 2
        assert quality.code_points(midpoint) == 50


class TestSyllables:
    """The syllable heuristic, which both Flesch formulas divide by."""

    @pytest.mark.parametrize("word,expected", [
        ("i", 1),
        ("see", 1),
        ("table", 2),
        ("code", 1),
        ("bicycle", 3),
        ("2026", 1),
        ("", 0),
    ])
    def test_syllable_counts(self, word, expected):
        assert quality.syllables(word) == expected


class TestProseLines:
    """Fenced blocks and markup removal for readability scoring."""

    def test_fenced_code_is_removed(self):
        body = "Some prose.\n\n```python\nx = 1\n```\n\nMore prose."
        lines = quality.prose_lines(body)
        joined = " ".join(lines)
        assert "x = 1" not in joined
        assert "Some prose" in joined

    def test_inline_code_is_removed(self):
        lines = quality.prose_lines("Use `pip install` to set up.")
        joined = " ".join(lines)
        assert "pip install" not in joined
        assert "Use" in joined

    def test_images_are_stripped(self):
        lines = quality.prose_lines("Before ![alt](http://img.png) after.")
        joined = " ".join(lines)
        assert "![" not in joined

    def test_empty_body_returns_empty(self):
        assert quality.prose_lines("") == []


class TestReport:
    """The combined score: weights, redistribution, and the empty-body override."""

    def _report(self, body="", **kwargs):
        defaults = {
            "title": "Test Title for Quality",
            "body_markdown": body,
            "meta_description": "A test meta description for quality scoring purposes.",
            "keywords": ["test"],
            "focus_keyword": "test",
            "slug": "test-title",
        }
        defaults.update(kwargs)
        return quality.report(**defaults)

    def test_weights_sum_to_one(self):
        assert quality.SEO_WEIGHT + quality.READABILITY_WEIGHT + quality.CODE_WEIGHT == 1.0

    def test_score_is_clamped_0_to_100(self):
        r = self._report(body="A " * 200)
        assert 0 <= r.score <= 100

    def test_report_as_dict_has_all_expected_keys(self):
        r = self._report(body="A paragraph " * 30)
        d = r.as_dict()
        expected = {
            "score", "seo_score", "reading_ease", "grade_level",
            "readability_points", "code_ratio", "code_points",
            "words", "sentences",
        }
        assert set(d.keys()) == expected

    def test_short_body_has_none_readability_points(self):
        r = self._report(body="Short.")
        assert r.readability_points is None
        assert r.readability.reading_ease is None

    def test_report_for_reads_content_attrs(self):
        class FakeContent:
            title = "A Piece"
            body_markdown = "Word " * 50
            meta_description = "Meta desc."
            keywords = ["kw"]
            cover_image_url = None
            focus_keyword = "word"
            slug = "a-piece"

        r = quality.report_for(FakeContent())
        assert isinstance(r, quality.QualityReport)
        assert r.score >= 0

    def test_report_for_handles_missing_attrs(self):
        class Empty:
            pass

        r = quality.report_for(Empty())
        assert r.score == 0
