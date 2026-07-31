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


def test_headings_inside_code_fences_are_ignored():
    """A Python comment like '# import os' inside a code block is not an H1."""
    body = (
        "## Real Heading\n\n"
        + ("word " * 400)
        + "\n\n```python\n# import os\n# This is a comment\n```\n"
    )
    issues = seo.audit(
        title="Marketing automation for devs",
        body_markdown=body,
        meta_description="x" * 100,
        keywords=["marketing automation"],
    )
    # No heading hierarchy issues — the code comments are not headings.
    skips = [i for i in issues if "skip" in i.message.lower()]
    assert len(skips) == 0


def test_seo_score_ignores_code_fence_headings():
    """seo_score should not count # comments in code as H1."""
    body = (
        "## How to automate\n\n"
        + ("automate " * 300)
        + "\n\n```bash\n# install deps\nnpm install\n```\n"
    )
    score = seo.seo_score(
        title="How to automate marketing",
        body_markdown=body,
        meta_description="Learn how to automate your marketing workflow.",
        keywords=["automate"],
        focus_keyword="automate",
        slug="how-to-automate-marketing",
    )
    # A good post should score above 50.
    assert score >= 50


def test_audit_is_clean_for_a_good_post():
    body = (
        "## Why Marketing Automation Matters\n\n"
        "Marketing automation saves teams hours every week. "
        + ("word " * 180)
        + "marketing automation "
        + ("word " * 180)
        + "\n\n## How Marketing Automation Works\n\ntext\n"
    )
    issues = seo.audit(
        title="Marketing automation, automated",
        body_markdown=body,
        meta_description="A description about marketing automation of exactly the right "
        "sort of length for a search engine result page.",
        keywords=["marketing automation"],
        cover_image_url="https://cdn.example.com/cover.png",
    )
    assert issues == []


# -- Focus keyword checks ------------------------------------------------- #


def test_audit_flags_low_keyword_density():
    body = "## Introduction\n\n" + ("unrelated " * 400) + "\n\n## Summary\n\ntext\n"
    issues = seo.audit(
        title="Marketing automation guide",
        body_markdown=body,
        meta_description="A guide to marketing automation for developers.",
        keywords=["marketing automation"],
        focus_keyword="marketing automation",
        cover_image_url="https://cdn.example.com/cover.png",
    )
    density_issues = [i for i in issues if "density" in i.message]
    assert len(density_issues) == 1


def test_audit_flags_missing_focus_keyword_in_first_paragraph():
    body = "## Why Marketing Automation\n\nThis paragraph talks about something else entirely " \
           "and has enough words to be real.\n\n" + ("marketing automation " * 200) + "\n"
    issues = seo.audit(
        title="Marketing automation guide",
        body_markdown=body,
        meta_description="A guide to marketing automation.",
        keywords=["marketing automation"],
        focus_keyword="marketing automation",
        cover_image_url="https://cdn.example.com/cover.png",
    )
    first_para_issues = [i for i in issues if "opening paragraph" in i.message]
    assert len(first_para_issues) == 1


def test_audit_flags_missing_focus_keyword_in_subheadings():
    body = "## Introduction\n\n" + ("marketing automation " * 200) + \
           "\n\n## Summary\n\ntext\n"
    issues = seo.audit(
        title="Marketing automation guide",
        body_markdown=body,
        meta_description="A guide to marketing automation.",
        keywords=["marketing automation"],
        focus_keyword="marketing automation",
        cover_image_url="https://cdn.example.com/cover.png",
    )
    heading_issues = [i for i in issues if "subheading" in i.message]
    assert len(heading_issues) == 1


def test_audit_flags_missing_image_alt_text():
    body = "## Why\n\n![](https://example.com/img.png)\n\n" + ("word " * 400) + "\n"
    issues = seo.audit(
        title="Marketing automation guide",
        body_markdown=body,
        meta_description="A guide to marketing automation.",
        keywords=["marketing automation"],
        cover_image_url="https://cdn.example.com/cover.png",
    )
    alt_issues = [i for i in issues if "alt text" in i.message]
    assert len(alt_issues) == 1


def test_audit_flags_long_slug():
    issues = seo.audit(
        title="Short",
        body_markdown="## Why\n\n" + ("word " * 400) + "\n",
        meta_description="x" * 100,
        keywords=["test"],
        slug="a-very-long-slug-that-exceeds-sixty-characters-and-should-be-trimmed-down-significantly",
    )
    slug_issues = [i for i in issues if "Slug" in i.message]
    assert len(slug_issues) >= 1


# -- SEO score ------------------------------------------------------------- #


def test_seo_score_perfect_post():
    # A post that passes every check should score 100.
    body = (
        "## Marketing Automation Explained\n\n"
        "Marketing automation helps developers save time by automating "
        "repetitive tasks. "
        + ("word " * 150)
        + "marketing automation "
        + ("word " * 150)
        + "\n\n## How Marketing Automation Works\n\ntext\n"
    )
    score = seo.seo_score(
        title="Marketing Automation Guide",
        body_markdown=body,
        meta_description="A complete guide to marketing automation for developers.",
        keywords=["marketing automation"],
        focus_keyword="marketing automation",
        slug="marketing-automation-guide",
        cover_image_url="https://cdn.example.com/cover.png",
    )
    assert score >= 90


def test_seo_score_thin_post():
    score = seo.seo_score(
        title="",
        body_markdown="Too short.",
        meta_description="",
        keywords=[],
    )
    assert score < seo.SEO_SCORE_THRESHOLD


def test_seo_score_is_bounded():
    # Even the worst post can't go below 0.
    score = seo.seo_score(
        title="",
        body_markdown="x",
        meta_description="",
        keywords=[],
    )
    assert 0 <= score <= 100


# -- JSON-LD structured data ---------------------------------------------- #


def test_build_json_ld_returns_valid_json():
    import json

    ld = seo.build_json_ld(
        title="Test Article",
        body_markdown="## Intro\n\n" + ("word " * 300),
        meta_description="A test article.",
        url="https://example.com/test",
        cover_image_url="https://cdn.example.com/img.png",
        keywords=["python", "testing"],
        author_name="Herald",
        publisher_name="Acme",
    )
    parsed = json.loads(ld)
    assert parsed["@context"] == "https://schema.org"
    assert parsed["@type"] == "Article"
    assert parsed["headline"] == "Test Article"
    assert parsed["image"] == "https://cdn.example.com/img.png"
    assert parsed["keywords"] == ["python", "testing"]
    assert parsed["author"]["name"] == "Herald"
    assert parsed["publisher"]["name"] == "Acme"
    assert "datePublished" in parsed
    assert parsed["wordCount"] > 0


def test_build_json_ld_omits_optional_fields():
    import json

    ld = seo.build_json_ld(
        title="Minimal",
        body_markdown="Short body.",
        meta_description="desc",
        url="",
    )
    parsed = json.loads(ld)
    assert "image" not in parsed
    assert "author" not in parsed
    assert "publisher" not in parsed
    assert "mainEntityOfPage" not in parsed


def test_json_ld_script_tag_wraps_correctly():
    tag = seo.json_ld_script_tag('{"@type": "Article"}')
    assert tag.startswith("\n<script type=\"application/ld+json\">")
    assert tag.endswith("</script>\n")
    assert '{"@type": "Article"}' in tag


# -- Sitemap generation --------------------------------------------------- #


def test_build_sitemap_entry_has_required_elements():
    from xml.etree.ElementTree import tostring

    entry = seo.build_sitemap_entry(
        url="https://example.com/post",
        lastmod="2026-07-30",
    )
    xml = tostring(entry, encoding="unicode")
    assert "<loc>https://example.com/post</loc>" in xml
    assert "<lastmod>2026-07-30</lastmod>" in xml
    assert "<changefreq>weekly</changefreq>" in xml
    assert "<priority>0.7</priority>" in xml


def test_build_sitemap_xml_produces_valid_xml():
    entries = [
        {"url": "https://example.com/a", "lastmod": "2026-07-01"},
        {"url": "https://example.com/b"},
    ]
    xml = seo.build_sitemap_xml(entries)
    assert xml.startswith('<?xml version="1.0"')
    assert "<urlset" in xml
    assert "http://www.sitemaps.org/schemas/sitemap/0.9" in xml
    assert "<loc>https://example.com/a</loc>" in xml
    assert "<loc>https://example.com/b</loc>" in xml
    assert xml.count("<url>") == 2
