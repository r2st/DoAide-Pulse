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


def test_density_counts_whole_words_not_substrings():
    """"api" inside "rapid" and "capital" is not a mention of the API.

    ``str.count`` found it anyway, which on a post that never discusses the API
    was enough on its own to clear the 0.5% floor — an audit note nobody could
    act on, because the occurrences it counted were not there.
    """
    body = "rapid capital rapids capitalise " * 25  # 100 words, zero real hits
    assert seo._keyword_occurrences(body, "api") == 0
    assert seo._keyword_density(body, "api") == 0.0

    real = "the api is documented " + ("filler " * 96)  # 1 hit in 100 words
    assert seo._keyword_occurrences(real, "api") == 1


def test_density_matches_a_keyword_that_ends_a_sentence():
    """Word edges, not whitespace: "the API." is still a mention."""
    assert seo._keyword_occurrences("We ship the API. Then the API, again.", "api") == 2


def test_density_matches_a_keyword_with_punctuation_in_it():
    """A developer audience's keywords include "c++" and ".net".

    A plain ``\\b`` on either side of the escaped keyword finds neither: the
    boundary it wants is a word/non-word transition, and there is none after
    the ``+`` of "c++" or before the ``.`` of ".net".
    """
    assert seo._keyword_occurrences("Written in c++ by hand.", "c++") == 1
    assert seo._keyword_occurrences("Targets .net and nothing else", ".net") == 1
    # And still not a substring: ".net" is not a mention of "asp.netcore".
    assert seo._keyword_occurrences("We use asp.netcore here", ".net") == 0


def test_density_counts_a_multi_word_keyword_for_its_own_length():
    """Two mentions of a two-word phrase is 4% of a hundred words, not 2%.

    Dividing phrase hits by a word count understated every multi-word focus
    keyword by exactly the number of words in it, so they always read as
    under-optimised no matter how often they appeared.
    """
    body = "content marketing " + ("filler " * 96) + "content marketing"
    assert seo._keyword_occurrences(body, "content marketing") == 2
    assert seo._keyword_density(body, "content marketing") == 4.0


def test_density_matches_a_phrase_across_a_line_break():
    body = "a piece about content\nmarketing and nothing else"
    assert seo._keyword_occurrences(body, "content marketing") == 1


def test_a_stuffed_substring_no_longer_reads_as_stuffing():
    """The same bug in the other direction: a false >3% "stuffing" warning."""
    body = (
        "## Rapid capital\n\n"
        + ("rapid capital rapids capitalise " * 100)
        + "\n\nThe api is mentioned once.\n"
    )
    issues = seo.audit(
        title="Api notes",
        body_markdown=body,
        meta_description="Notes on the api and how it is put together here.",
        keywords=["api"],
        focus_keyword="api",
        cover_image_url="https://cdn.example.com/cover.png",
    )
    assert not [i for i in issues if "stuffing" in i.message]


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
        author_name="Pulse",
        publisher_name="Acme",
    )
    parsed = json.loads(ld)
    assert parsed["@context"] == "https://schema.org"
    assert parsed["@type"] == "Article"
    assert parsed["headline"] == "Test Article"
    assert parsed["image"] == "https://cdn.example.com/img.png"
    assert parsed["keywords"] == ["python", "testing"]
    assert parsed["author"]["name"] == "Pulse"
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


def test_suggest_internal_links_ranks_by_shared_keyword_count():
    candidates = [
        {
            "content_id": 1,
            "title": "Celery retries",
            "slug": "celery-retries",
            "keywords": ["celery", "retries", "async"],
        },
        {
            "content_id": 2,
            "title": "Async in FastAPI",
            "slug": "async-fastapi",
            "keywords": ["async", "fastapi"],
        },
        {
            "content_id": 3,
            "title": "Unrelated",
            "slug": "unrelated",
            "keywords": ["docker"],
        },
    ]
    out = seo.suggest_internal_links(
        keywords=["celery", "async", "retries"], candidates=candidates
    )
    assert [s["content_id"] for s in out] == [1, 2]
    assert out[0]["matched_keywords"] == ["async", "celery", "retries"]
    assert out[0]["score"] == 3


def test_suggest_internal_links_focus_keyword_counts_as_a_keyword():
    candidates = [
        {
            "content_id": 1,
            "title": "Deep dive",
            "slug": "deep-dive",
            "keywords": [],
            "focus_keyword": "observability",
        }
    ]
    out = seo.suggest_internal_links(
        keywords=[], focus_keyword="observability", candidates=candidates
    )
    assert len(out) == 1
    assert out[0]["matched_keywords"] == ["observability"]
    # Own overlap (+1) plus the "both pieces are focused on this" bonus (+1).
    assert out[0]["score"] == 2


def test_suggest_internal_links_drops_zero_overlap_candidates():
    candidates = [
        {"content_id": 1, "title": "X", "slug": "x", "keywords": ["docker"]},
    ]
    out = seo.suggest_internal_links(keywords=["kubernetes"], candidates=candidates)
    assert out == []


def test_suggest_internal_links_respects_limit():
    candidates = [
        {"content_id": i, "title": f"P{i}", "slug": f"p{i}", "keywords": ["shared"]}
        for i in range(10)
    ]
    out = seo.suggest_internal_links(keywords=["shared"], candidates=candidates, limit=3)
    assert len(out) == 3


def test_suggest_internal_links_with_no_own_keywords_returns_nothing():
    candidates = [
        {"content_id": 1, "title": "X", "slug": "x", "keywords": ["docker"]},
    ]
    assert seo.suggest_internal_links(keywords=[], candidates=candidates) == []


def test_suggest_internal_links_includes_canonical_url():
    candidates = [
        {
            "content_id": 1,
            "title": "X",
            "slug": "x",
            "keywords": ["docker"],
            "canonical_url": "https://example.com/x",
        },
    ]
    out = seo.suggest_internal_links(keywords=["docker"], candidates=candidates)
    assert out[0]["url"] == "https://example.com/x"


# --------------------------------------------------------------------------- #
# The audit and the score have to agree                                        #
# --------------------------------------------------------------------------- #
#
# Every deduction ``seo_score`` makes should be findable in ``audit``. The panel
# is the only place an author can see *what* to fix; a number that drops for a
# reason the panel never states is a number they cannot act on.


def _fields(issues) -> set[str]:
    return {issue.field for issue in issues}


def test_a_focus_keyword_missing_from_the_title_is_reported_without_a_keyword_list():
    """An explicit focus keyword is checked whether or not `keywords` is set.

    The two checks used to be chained, so a piece carrying `focus_keyword` and an
    empty `keywords` list reported only "no keywords set" — while `seo_score`
    docked ten points for the title on its own. Panel clean, score 90, nothing to
    click.
    """
    issues = seo.audit(
        title="Shipping faster with less ceremony",
        body_markdown="## Intro\n\n" + ("word " * 400),
        meta_description="A description of the thing, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=[],
        focus_keyword="celery retries",
    )

    assert "keywords" in _fields(issues)
    title_issues = [i for i in issues if i.field == "title"]
    assert any("celery retries" in i.message for i in title_issues)


def test_the_title_check_is_silent_when_the_focus_keyword_is_in_the_title():
    issues = seo.audit(
        title="Celery retries, explained",
        body_markdown="## Celery retries\n\n" + ("celery retries " * 20) + ("word " * 300),
        meta_description="How celery retries work, at length, in a description long "
        "enough to clear the minimum the audit enforces.",
        keywords=[],
        focus_keyword="celery retries",
    )

    assert not [i for i in issues if i.field == "title"]


def test_every_score_deduction_for_the_title_has_an_audit_issue_behind_it():
    """The regression stated as the invariant, not as one example."""
    kwargs = dict(
        title="Shipping faster with less ceremony",
        body_markdown="## Intro\n\n" + ("word " * 400),
        meta_description="A description of the thing, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=[],
        focus_keyword="celery retries",
    )

    docked = 100 - seo.seo_score(**kwargs)
    assert docked > 0
    # Whatever the score took off, the panel names at least one thing to do.
    assert seo.audit(**kwargs)


def test_the_near_miss_density_band_is_named_rather_than_only_charged_for():
    """0.5-1% and 2.5-3% cost five points, and used to cost them silently.

    `seo_score` has three density bands and `audit` had two. A piece sitting in
    the middle one showed a clean panel and a score of 95 — the same
    unactionable number the focus-keyword-in-title fix was about, in the check
    immediately below it.
    """
    body = "## Celery retries in practice\n\nCelery retries are the subject here. " + (
        "word " * 600
    )
    kwargs = dict(
        title="Celery retries, explained",
        body_markdown=body,
        meta_description="How celery retries work, at length, in a description long "
        "enough to clear the minimum the audit enforces.",
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
        cover_image_url="https://example.com/cover.png",
    )

    # Squarely inside the band the score charges for and nothing else.
    density = seo._keyword_density(seo.strip_markdown(body), "celery retries")
    assert 0.5 <= density < 1.0
    assert seo.seo_score(**kwargs) == 95

    body_issues = [i for i in seo.audit(**kwargs) if i.field == "body"]
    assert len(body_issues) == 1
    assert "0.7%" in body_issues[0].message


def test_a_density_inside_the_target_range_is_still_silent():
    """The band that scores full marks says nothing — otherwise the panel is noise."""
    body = "## Celery retries in practice\n\nCelery retries are the subject here, and "
    body += "celery retries again. " + ("word " * 300)
    kwargs = dict(
        title="Celery retries, explained",
        body_markdown=body,
        meta_description="How celery retries work, at length, in a description long "
        "enough to clear the minimum the audit enforces.",
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
        cover_image_url="https://example.com/cover.png",
    )

    density = seo._keyword_density(seo.strip_markdown(body), "celery retries")
    assert 1.0 <= density <= 2.5, density
    assert seo.seo_score(**kwargs) == 100
    assert seo.audit(**kwargs) == []


def test_every_density_band_the_score_charges_for_has_an_audit_issue_behind_it():
    """The invariant, swept across the whole range rather than at one point."""
    base = dict(
        title="Celery retries, explained",
        meta_description="How celery retries work, at length, in a description long "
        "enough to clear the minimum the audit enforces.",
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
        cover_image_url="https://example.com/cover.png",
    )
    for mentions in range(1, 12):
        body = "## Celery retries in practice\n\n"
        body += "Celery retries. " * mentions
        body += "word " * 300
        kwargs = {**base, "body_markdown": body}

        if seo.seo_score(**kwargs) < 100:
            assert [i for i in seo.audit(**kwargs) if i.field == "body"], (
                f"{mentions} mentions: score docked, panel silent"
            )


# -- Whole-word focus-keyword matching ------------------------------------- #
#
# `_keyword_density` has counted whole words since the "api inside rapid" fix.
# Every *other* focus-keyword check went on using `in` — plain substring — so
# the two disagreed about the same keyword in the same piece, and the
# disagreement always fell the same way: the substring checks stayed silent
# about a keyword the piece does not actually use.


def test_a_title_that_only_contains_the_keyword_as_a_substring_is_flagged():
    """"Rapid" contains "api". The title does not mention the API."""
    issues = seo.audit(
        title="Rapid prototyping for teams",
        body_markdown="## Intro\n\n" + ("word " * 400),
        meta_description="A description of the thing, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=["api"],
        focus_keyword="api",
    )

    title_issues = [i for i in issues if i.field == "title"]
    assert any('"api" does not appear in the title' in i.message for i in title_issues)


def test_a_title_that_really_contains_the_keyword_is_still_silent():
    issues = seo.audit(
        title="The API, explained",
        body_markdown="## The API\n\nThe API is the subject. " + ("word " * 400),
        meta_description="A description of the API, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=["api"],
        focus_keyword="api",
    )

    assert not [i for i in issues if i.field == "title"]


def test_a_slug_that_only_contains_the_keyword_as_a_substring_is_flagged():
    issues = seo.audit(
        title="The API, explained",
        body_markdown="## The API\n\nThe API is the subject. " + ("word " * 400),
        meta_description="A description of the API, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=["api"],
        focus_keyword="api",
        slug="rapid-prototyping-for-teams",
    )

    assert [i for i in issues if i.field == "slug"]


def test_a_hyphenated_slug_still_matches_a_multi_word_keyword():
    """Hyphens are the slug's word separator — the match must see through them."""
    issues = seo.audit(
        title="Celery retries, explained",
        body_markdown="## Celery retries\n\nCelery retries are the subject. "
        + ("word " * 400),
        meta_description="How celery retries work, at length, in a description long "
        "enough to clear the minimum the audit enforces.",
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
    )

    assert not [i for i in issues if i.field == "slug"]


def test_a_substring_only_keyword_costs_the_same_points_the_panel_names():
    """The invariant: audit and score agree about what counts as a mention."""
    kwargs = dict(
        title="Rapid prototyping for teams",
        body_markdown="## Rapid prototyping\n\nRapid prototyping is the subject. "
        + ("word " * 400),
        meta_description="A description of rapid prototyping, long enough to pass "
        "the floor that the audit applies to meta descriptions.",
        keywords=["api"],
        focus_keyword="api",
        slug="rapid-prototyping-for-teams",
        cover_image_url="https://example.com/cover.png",
    )

    # Nothing in this piece mentions the API as a word, so every focus-keyword
    # check should fire — and the score should charge for each one.
    assert seo.seo_score(**kwargs) < 70
    fields = {i.field for i in seo.audit(**kwargs)}
    assert {"title", "body", "meta_description", "slug"} <= fields


def test_the_subheading_check_reads_whole_words_too():
    issues = seo.audit(
        title="The API, explained",
        body_markdown="## Rapid prototyping\n\nThe API is the subject here. "
        + ("word " * 400),
        meta_description="A description of the API, long enough to pass the floor "
        "that the audit applies to meta descriptions.",
        keywords=["api"],
        focus_keyword="api",
    )

    assert any("not in any subheading" in i.message for i in issues)


# -- Over-long meta descriptions ------------------------------------------- #


def test_an_over_long_meta_description_is_flagged():
    """The field accepts 500 characters; nothing checked the upper bound.

    `build_meta_description` trims what the model returns, so this was
    unreachable for generated content — but the editor can type into the field,
    and a 300-character description scored 100 while Google clipped it.
    """
    long_meta = "A description of celery retries that runs on and on. " * 6
    assert len(long_meta) > seo.META_DESCRIPTION_MAX

    issues = seo.audit(
        title="Celery retries, explained",
        body_markdown="## Celery retries\n\nCelery retries are the subject. "
        + ("word " * 400),
        meta_description=long_meta,
        keywords=["celery retries"],
        focus_keyword="celery retries",
    )

    meta_issues = [i for i in issues if i.field == "meta_description"]
    assert any("cut off around" in i.message for i in meta_issues)


def test_an_over_long_meta_description_costs_points():
    base = dict(
        title="Celery retries, explained",
        body_markdown="## Celery retries\n\nCelery retries are the subject. "
        + ("celery retries " * 6)
        + ("word " * 400),
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
        cover_image_url="https://example.com/cover.png",
    )
    good = "How celery retries work, at length, in a description long enough to "
    good += "clear the minimum the audit enforces."
    long_meta = "A description of celery retries that runs on and on. " * 6

    assert seo.META_DESCRIPTION_MIN <= len(good) <= seo.META_DESCRIPTION_MAX
    assert seo.seo_score(**base, meta_description=good) > seo.seo_score(
        **base, meta_description=long_meta
    )


def test_a_meta_description_that_is_both_short_and_off_keyword_is_charged_for_both():
    """Folded into one `or`, the two cost five points between them.

    Two audit issues, one deduction — the panel named more to fix than the
    score had taken off, which is the same disagreement the other way round.
    """
    base = dict(
        title="Celery retries, explained",
        body_markdown="## Celery retries\n\nCelery retries are the subject. "
        + ("celery retries " * 6)
        + ("word " * 400),
        keywords=["celery retries"],
        focus_keyword="celery retries",
        slug="celery-retries-explained",
        cover_image_url="https://example.com/cover.png",
    )
    short_and_off = "A short note."
    short_on_keyword = "Celery retries."

    assert len(short_and_off) < seo.META_DESCRIPTION_MIN
    assert len(short_on_keyword) < seo.META_DESCRIPTION_MIN

    both = seo.seo_score(**base, meta_description=short_and_off)
    one = seo.seo_score(**base, meta_description=short_on_keyword)
    assert one - both == 5

    issues = seo.audit(**base, meta_description=short_and_off)
    assert len([i for i in issues if i.field == "meta_description"]) == 2


# -- Keyword length -------------------------------------------------------- #


def test_an_absurdly_long_keyword_is_dropped():
    """`focus_keyword` is capped at 100 chars on the schema; `keywords` was not.

    `app.routers.content` fills a missing focus keyword from `keywords[0]`,
    which never passed the field validator — so an over-long list entry became
    an over-long focus keyword, and then the body of a regex run over the whole
    post on every audit.
    """
    huge = "a" * (seo.KEYWORD_MAX_LENGTH + 1)
    assert seo.normalize_keywords([huge, "celery retries"]) == ["celery retries"]


def test_a_keyword_at_the_length_limit_is_kept():
    at_limit = "a" * seo.KEYWORD_MAX_LENGTH
    assert seo.normalize_keywords([at_limit]) == [at_limit]
