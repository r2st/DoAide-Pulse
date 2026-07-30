from __future__ import annotations

from app.services import seo


def test_truncate_prefers_a_sentence_boundary():
    text = "First sentence here. Second sentence that would overflow the limit."
    assert seo.truncate_at_sentence(text, 40) == "First sentence here."


def test_truncate_falls_back_to_a_word_boundary():
    text = "supercalifragilistic " * 10
    out = seo.truncate_at_sentence(text, 50)
    assert len(out) <= 50
    assert out.endswith("…")
    # Never mid-word.
    assert "supercalifragilisti…" not in out


def test_meta_description_is_capped():
    body = "A sentence about the thing. " * 40
    meta = seo.build_meta_description("", fallback_body=body)
    assert len(meta) <= seo.META_DESCRIPTION_MAX


def test_normalize_keywords_dedupes_and_caps():
    out = seo.normalize_keywords(
        ["FastAPI", "fastapi", "  React  ", "a", "7", ""], extra=["celery"]
    )
    assert out == ["fastapi", "react", "celery"]
    assert len(seo.normalize_keywords([f"kw{i}" for i in range(20)])) == seo.KEYWORD_MAX


def test_strip_markdown_removes_code_and_links():
    md = "See [the docs](https://x.com) and:\n\n```python\nprint('hi')\n```\n"
    text = seo.strip_markdown(md)
    assert "the docs" in text
    assert "https://x.com" not in text
    assert "print" not in text


def test_excerpt_skips_the_heading():
    md = "# Title\n\nShort.\n\nThis paragraph is long enough to be a real excerpt, "
    md += "with more than forty characters in it."
    assert seo.build_excerpt(md).startswith("This paragraph is long enough")


def test_audit_flags_a_thin_post():
    issues = seo.audit(
        title="", body_markdown="Too short.", meta_description="", keywords=[]
    )
    fields = {i.field for i in issues}
    assert {"title", "meta_description", "keywords", "body"} <= fields
    # Errors sort first.
    assert issues[0].level == "error"


def test_audit_flags_a_skipped_heading_level_once():
    body = "## A\n\n" + ("word " * 400) + "\n\n#### B\n\ntext\n\n###### C\n"
    issues = seo.audit(
        title="Marketing automation for devs",
        body_markdown=body,
        meta_description="x" * 100,
        keywords=["marketing automation"],
    )
    skips = [i for i in issues if "skip" in i.message]
    assert len(skips) == 1


def test_audit_is_clean_for_a_good_post():
    body = "## Why\n\n" + ("word " * 400) + "\n\n## How\n\ntext\n"
    issues = seo.audit(
        title="Marketing automation, automated",
        body_markdown=body,
        meta_description="A description of exactly the right sort of length for a "
        "search engine result page to show in full.",
        keywords=["marketing automation"],
        cover_image_url="https://cdn.example.com/cover.png",
    )
    assert issues == []
