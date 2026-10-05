"""H027 — computation correctness, M2 pass 4.

Two findings:

1. ``truncate_at_sentence`` charged a phantom space for the first sentence,
   rejecting a sentence that fit exactly at the limit.
2. The git publisher used 238 wpm for reading time while ``read_minutes_for``
   uses 220 wpm — the same piece showed different reading times on the
   analytics dashboard and in its git front matter.
"""
from app.models.content import read_minutes_for
from app.services.seo import truncate_at_sentence


# ── Finding 1: truncate_at_sentence off-by-one ─────────────────────── #


class TestTruncateFirstSentenceExactFit:
    """The first sentence should be kept when it fits exactly at the limit."""

    def test_first_sentence_at_limit_is_kept(self):
        text = "Hello world ab. More stuff here and beyond."
        assert truncate_at_sentence(text, 15) == "Hello world ab."

    def test_first_sentence_one_over_falls_back(self):
        text = "Hello world abc. More stuff here and beyond."
        result = truncate_at_sentence(text, 15)
        assert result != "Hello world abc."
        assert len(result) <= 15

    def test_two_sentences_exact_fit(self):
        text = "Hi. Yo. And more words follow here."
        # "Hi. Yo." is 7 characters; "Hi." is 3 + space + "Yo." is 3 = 7
        assert truncate_at_sentence(text, 7) == "Hi. Yo."

    def test_text_within_limit_returned_whole(self):
        assert truncate_at_sentence("Short.", 100) == "Short."

    def test_empty_text(self):
        assert truncate_at_sentence("", 10) == ""

    def test_single_sentence_over_limit_uses_ellipsis(self):
        text = "Supercalifragilisticexpialidocious word"
        result = truncate_at_sentence(text, 15)
        assert result.endswith("…")
        assert len(result) <= 15


# ── Finding 2: reading-time WPM consistency ────────────────────────── #


class TestReadingTimeConsistency:
    """The git front matter must use the same formula as read_minutes_for."""

    def test_canonical_formula_at_220_wpm(self):
        assert read_minutes_for(220) == 1
        assert read_minutes_for(440) == 2
        assert read_minutes_for(0) == 1  # floored at 1

    def test_git_publisher_uses_canonical_formula(self):
        """The git publisher must import read_minutes_for, not hardcode its own rate.

        Before this fix it used ``max(1, round(word_count / 238))`` which gives
        a different reading time for the same piece — e.g. 1000 words → 5 min
        (220 wpm) vs 4 min (238 wpm).
        """
        from app.services.publishers import git

        assert hasattr(git, "read_minutes_for"), (
            "git publisher should import read_minutes_for"
        )

    def test_1000_words_same_everywhere(self):
        assert read_minutes_for(1000) == 5  # round(1000/220) = round(4.545) = 5

    def test_short_post_floored_at_one_minute(self):
        assert read_minutes_for(50) == 1
        assert read_minutes_for(1) == 1
