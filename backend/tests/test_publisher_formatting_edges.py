"""The shared publisher formatting helpers, on the inputs that are not ordinary.

Every adapter runs its body through these before it goes out, so a wrong answer
here is wrong on every platform at once: a hard line break flattened into a
space, a control character written raw into YAML front matter that a static-site
build then refuses, or a LinkedIn post cut mid-word.
"""
from __future__ import annotations

from app.services.publishers import formatting

# --------------------------------------------------------------------------- #
# Plain text                                                                  #
# --------------------------------------------------------------------------- #


def test_a_hard_line_break_survives_as_a_break_rather_than_a_space():
    """Two trailing spaces is Markdown for <br>. Verse and addresses need it."""
    assert "<br" in formatting.to_html("Line one  \nLine two")

    text = formatting.to_plain_text("Line one  \nLine two")

    assert "Line one Line two" not in text
    assert text.splitlines()[0] == "Line one"
    assert text.splitlines()[-1] == "Line two"


def test_block_elements_keep_their_separation_in_plain_text():
    text = formatting.to_plain_text("# Title\n\nFirst para.\n\n- a\n- b")

    assert "Title" in text
    assert "First para." in text
    assert "\n" in text


# --------------------------------------------------------------------------- #
# YAML front matter escaping                                                  #
# --------------------------------------------------------------------------- #


def test_a_c1_control_character_is_escaped_rather_than_written_raw():
    """C0 and C1 are outside YAML's printable set; a raw one breaks the build."""
    escaped = formatting.escape_yaml_scalar("Title\x9bhere")

    assert "\\u009b" in escaped
    assert "\x9b" not in escaped


def test_a_c0_control_character_uses_the_short_escape():
    escaped = formatting.escape_yaml_scalar("Title\x01here")

    assert "\\x01" in escaped
    assert "\x01" not in escaped


# --------------------------------------------------------------------------- #
# LinkedIn                                                                    #
# --------------------------------------------------------------------------- #


def test_a_post_past_linkedin_s_hard_limit_is_cut_on_a_word_boundary():
    body = " ".join(["word"] * 2000)

    out = formatting.truncate_for_linkedin(body)

    assert len(out) <= formatting.LINKEDIN_HARD_LIMIT
    assert out.endswith("…")
    # Cut at a space, so the last thing a reader sees is a whole word.
    assert out[:-1].rstrip().endswith("word")


def test_the_link_keeps_its_own_line_and_its_room_in_the_budget():
    url = "https://example.com/a-fairly-long-canonical-url"
    body = " ".join(["word"] * 2000)

    out = formatting.truncate_for_linkedin(body, url=url)

    assert len(out) <= formatting.LINKEDIN_HARD_LIMIT
    assert out.endswith(f"\n\n{url}")


def test_a_body_with_no_spaces_to_cut_on_is_cut_anyway():
    """``rfind`` finds nothing; the clip still has to land under the limit."""
    out = formatting.truncate_for_linkedin("x" * (formatting.LINKEDIN_HARD_LIMIT + 500))

    assert len(out) <= formatting.LINKEDIN_HARD_LIMIT
    assert out.endswith("…")


# --------------------------------------------------------------------------- #
# Tags                                                                        #
# --------------------------------------------------------------------------- #


def test_tags_that_clean_down_to_nothing_or_to_a_duplicate_are_dropped():
    out = formatting.normalize_tags(
        ["Python", "---", "python", "  ", "FastAPI"], limit=10
    )

    assert out == ["python", "fastapi"]


def test_tags_are_capped_at_the_platform_s_limit():
    out = formatting.normalize_tags(["a", "b", "c", "d", "e"], limit=3)

    assert out == ["a", "b", "c"]
