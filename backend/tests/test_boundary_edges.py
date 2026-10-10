"""H054 M1: Boundary/edge-case testing across Pulse's core modules.

Each section picks a module, enumerates its inputs, and tests at the boundaries
where assumptions about 'reasonable input' break: zero, negative, empty string,
None, single-element arrays, max-length strings, and unicode.
"""
from __future__ import annotations

import pytest


# ─── seo.truncate_at_sentence ───────────────────────────────────────────── #

class TestTruncateAtSentence:
    """Boundaries for seo.truncate_at_sentence."""

    def test_limit_zero_returns_empty(self):
        from app.services.seo import truncate_at_sentence

        result = truncate_at_sentence("Hello world. This is a test.", 0)
        assert result == ""

    def test_limit_negative_returns_empty(self):
        from app.services.seo import truncate_at_sentence

        result = truncate_at_sentence("Hello world.", -5)
        assert result == ""

    def test_limit_one_returns_at_most_one_char(self):
        from app.services.seo import truncate_at_sentence

        result = truncate_at_sentence("Hello world.", 1)
        assert len(result) <= 1

    def test_empty_text_returns_empty(self):
        from app.services.seo import truncate_at_sentence

        assert truncate_at_sentence("", 100) == ""

    def test_whitespace_only_text(self):
        from app.services.seo import truncate_at_sentence

        assert truncate_at_sentence("   ", 100) == ""

    def test_text_exactly_at_limit(self):
        from app.services.seo import truncate_at_sentence

        text = "Exact."
        assert truncate_at_sentence(text, len(text)) == text

    def test_text_one_over_limit(self):
        from app.services.seo import truncate_at_sentence

        result = truncate_at_sentence("Hello world.", 11)
        assert len(result) <= 11

    def test_single_long_word_no_spaces(self):
        from app.services.seo import truncate_at_sentence

        result = truncate_at_sentence("abcdefghijklmnop", 10)
        assert len(result) <= 10


# ─── seo.normalize_keywords ─────────────────────────────────────────────── #

class TestNormalizeKeywords:
    """Boundaries for seo.normalize_keywords."""

    def test_empty_list(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords([]) == []

    def test_single_char_keyword_dropped(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords(["x"]) == []

    def test_digit_only_keyword_dropped(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords(["42"]) == []

    def test_over_max_length_keyword_dropped(self):
        from app.services.seo import KEYWORD_MAX_LENGTH, normalize_keywords

        long_kw = "a" * (KEYWORD_MAX_LENGTH + 1)
        assert normalize_keywords([long_kw]) == []

    def test_exactly_max_length_keyword_kept(self):
        from app.services.seo import KEYWORD_MAX_LENGTH, normalize_keywords

        exact_kw = "a" * KEYWORD_MAX_LENGTH
        assert normalize_keywords([exact_kw]) == [exact_kw]

    def test_duplicate_keywords_deduped(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords(["api", "API", "Api"]) == ["api"]

    def test_whitespace_only_keyword_dropped(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords(["   "]) == []

    def test_keywords_capped_at_max(self):
        from app.services.seo import KEYWORD_MAX, normalize_keywords

        many = [f"keyword{i}" for i in range(KEYWORD_MAX + 5)]
        result = normalize_keywords(many)
        assert len(result) == KEYWORD_MAX

    def test_hash_and_punctuation_stripped(self):
        from app.services.seo import normalize_keywords

        assert normalize_keywords(["#python,", ".rust."]) == ["python", "rust"]


# ─── seo.build_excerpt ──────────────────────────────────────────────────── #

class TestBuildExcerpt:
    """Boundaries for seo.build_excerpt."""

    def test_empty_body(self):
        from app.services.seo import build_excerpt

        assert build_excerpt("") == ""

    def test_all_headings_body(self):
        from app.services.seo import build_excerpt

        body = "# Title\n\n## Heading\n\n### Sub"
        result = build_excerpt(body)
        assert isinstance(result, str)

    def test_short_paragraphs_under_40_chars(self):
        from app.services.seo import build_excerpt

        body = "Hi.\n\nOk.\n\n" + "A" * 50 + " real paragraph here."
        result = build_excerpt(body)
        assert len(result) > 0


# ─── seo.seo_score ──────────────────────────────────────────────────────── #

class TestSeoScore:
    """Boundaries for seo.seo_score."""

    def test_all_empty_inputs(self):
        from app.services.seo import seo_score

        score = seo_score(
            title="",
            body_markdown="",
            meta_description="",
            keywords=[],
        )
        assert 0 <= score <= 100

    def test_perfect_input(self):
        from app.services.seo import seo_score

        words = " ".join(["testing"] * 150 + ["other"] * 150)
        body = f"## Testing\n\n{words}"
        score = seo_score(
            title="Testing Best Practices",
            body_markdown=body,
            meta_description="A comprehensive guide to testing best practices for developers.",
            keywords=["testing", "best practices"],
            focus_keyword="testing",
            slug="testing-best-practices",
            cover_image_url="https://example.com/img.jpg",
        )
        assert score >= 80

    def test_whitespace_only_focus_keyword(self):
        from app.services.seo import seo_score

        score = seo_score(
            title="Hello",
            body_markdown="Some words here.",
            meta_description="Description",
            keywords=[],
            focus_keyword="   ",
        )
        assert 0 <= score <= 100


# ─── quality.syllables ──────────────────────────────────────────────────── #

class TestSyllables:
    """Boundaries for quality.syllables."""

    def test_empty_string(self):
        from app.services.quality import syllables

        assert syllables("") == 0

    def test_single_vowel(self):
        from app.services.quality import syllables

        assert syllables("a") == 1

    def test_no_vowels(self):
        from app.services.quality import syllables

        assert syllables("rhythm") >= 1

    def test_number_string(self):
        from app.services.quality import syllables

        assert syllables("123") >= 1

    def test_apostrophe_word(self):
        from app.services.quality import syllables

        assert syllables("don't") >= 1

    def test_silent_e(self):
        from app.services.quality import syllables

        assert syllables("code") == 1

    def test_table_not_silent_e(self):
        from app.services.quality import syllables

        assert syllables("table") == 2


# ─── quality.readability ────────────────────────────────────────────────── #

class TestReadability:
    """Boundaries for quality.readability."""

    def test_empty_body(self):
        from app.services.quality import readability

        r = readability("")
        assert r.words == 0
        assert r.sentences == 0
        assert r.reading_ease is None
        assert r.grade_level is None

    def test_under_min_words(self):
        from app.services.quality import MIN_WORDS_FOR_READABILITY, readability

        body = " ".join(["word"] * (MIN_WORDS_FOR_READABILITY - 1))
        r = readability(body)
        assert r.reading_ease is None

    def test_exactly_min_words(self):
        from app.services.quality import MIN_WORDS_FOR_READABILITY, readability

        body = ". ".join(["The quick brown fox jumps"] * (MIN_WORDS_FOR_READABILITY // 5 + 1))
        r = readability(body)
        if r.words >= MIN_WORDS_FOR_READABILITY:
            assert r.reading_ease is not None

    def test_all_code_body(self):
        from app.services.quality import readability

        body = "```python\nprint('hello')\n```"
        r = readability(body)
        assert r.words == 0

    def test_none_body(self):
        from app.services.quality import readability

        r = readability(None)
        assert r.words == 0


# ─── quality.code_ratio ─────────────────────────────────────────────────── #

class TestCodeRatio:
    """Boundaries for quality.code_ratio."""

    def test_empty_body(self):
        from app.services.quality import code_ratio

        assert code_ratio("") == 0.0

    def test_none_body(self):
        from app.services.quality import code_ratio

        assert code_ratio(None) == 0.0

    def test_all_code(self):
        from app.services.quality import code_ratio

        body = "```\ncode here\n```"
        ratio = code_ratio(body)
        assert ratio > 0.5

    def test_no_code(self):
        from app.services.quality import code_ratio

        body = "This is plain text with no code at all."
        assert code_ratio(body) == 0.0

    def test_inline_code_only(self):
        from app.services.quality import code_ratio

        body = "Use `print()` to output and `len()` to measure."
        ratio = code_ratio(body)
        assert 0.0 < ratio < 1.0

    def test_unclosed_fence(self):
        from app.services.quality import code_ratio

        body = "Some text\n```\ncode with no closing fence"
        ratio = code_ratio(body)
        assert ratio > 0.0


# ─── quality.readability_points ──────────────────────────────────────────── #

class TestReadabilityPoints:
    """Boundaries for quality.readability_points."""

    def test_none_input(self):
        from app.services.quality import readability_points

        assert readability_points(None) is None

    def test_exactly_at_min_target(self):
        from app.services.quality import READING_EASE_TARGET_MIN, readability_points

        assert readability_points(READING_EASE_TARGET_MIN) == 100

    def test_exactly_at_max_target(self):
        from app.services.quality import READING_EASE_TARGET_MAX, readability_points

        assert readability_points(READING_EASE_TARGET_MAX) == 100

    def test_reading_ease_zero(self):
        from app.services.quality import readability_points

        result = readability_points(0.0)
        assert result == 20

    def test_reading_ease_100(self):
        from app.services.quality import READING_EASE_TARGET_MAX, readability_points

        result = readability_points(100.0)
        assert result == max(0, round(100 - (100.0 - READING_EASE_TARGET_MAX)))


# ─── quality.code_points ─────────────────────────────────────────────────── #

class TestCodePoints:
    """Boundaries for quality.code_points."""

    def test_zero_ratio(self):
        from app.services.quality import code_points

        assert code_points(0.0) == 100

    def test_exactly_ideal_max(self):
        from app.services.quality import CODE_RATIO_IDEAL_MAX, code_points

        assert code_points(CODE_RATIO_IDEAL_MAX) == 100

    def test_ratio_1_0(self):
        from app.services.quality import code_points

        assert code_points(1.0) == 0

    def test_just_over_ideal(self):
        from app.services.quality import CODE_RATIO_IDEAL_MAX, code_points

        result = code_points(CODE_RATIO_IDEAL_MAX + 0.01)
        assert 95 <= result < 100


# ─── quality.report ──────────────────────────────────────────────────────── #

class TestQualityReport:
    """Boundaries for quality.report."""

    def test_all_empty(self):
        from app.services.quality import report

        r = report(
            title="",
            body_markdown="",
            meta_description="",
            keywords=[],
        )
        assert r.score == 0

    def test_whitespace_only_body(self):
        from app.services.quality import report

        r = report(
            title="Title",
            body_markdown="   \n\n   ",
            meta_description="Description here.",
            keywords=["test"],
        )
        assert r.score == 0


# ─── dedup.tokens ────────────────────────────────────────────────────────── #

class TestDedupTokens:
    """Boundaries for dedup.tokens."""

    def test_empty_string(self):
        from app.services.dedup import tokens

        assert tokens("") == frozenset()

    def test_none_input(self):
        from app.services.dedup import tokens

        assert tokens(None) == frozenset()

    def test_all_stopwords(self):
        from app.services.dedup import tokens

        assert tokens("the and or with") == frozenset()

    def test_unicode_emoji(self):
        from app.services.dedup import tokens

        result = tokens("🚀 Launch day")
        assert "launch" in result

    def test_possessive(self):
        from app.services.dedup import tokens

        assert tokens("Pulse's caching") == tokens("Pulse caching")

    def test_version_number(self):
        from app.services.dedup import tokens

        result = tokens("Pulse 2.0 released")
        assert "2.0" in result


# ─── dedup.is_restatement ────────────────────────────────────────────────── #

class TestIsRestatement:
    """Boundaries for dedup.is_restatement."""

    def test_empty_candidate(self):
        from app.services.dedup import is_restatement

        assert is_restatement("", frozenset({"pulse", "deploy"})) is False

    def test_empty_existing(self):
        from app.services.dedup import is_restatement

        assert is_restatement("Deploy Pulse", frozenset()) is False

    def test_single_word_exact_match(self):
        from app.services.dedup import is_restatement, tokens

        assert is_restatement("Caching", tokens("Caching")) is True

    def test_single_word_different(self):
        from app.services.dedup import is_restatement, tokens

        assert is_restatement("Caching", tokens("Deploying")) is False

    def test_below_similarity_threshold(self):
        from app.services.dedup import is_restatement, tokens

        existing = tokens("Getting started with Pulse Pro features today")
        assert is_restatement("Getting started with Another Tool", existing) is False


# ─── feeds._entry_id ────────────────────────────────────────────────────── #

class TestFeedEntryId:
    """Boundaries for feeds._entry_id."""

    def test_empty_guid_falls_to_hash(self):
        from app.services.feeds import _entry_id

        result = _entry_id("", "https://example.com", "Title")
        assert result.startswith("h:")

    def test_whitespace_guid_falls_to_hash(self):
        from app.services.feeds import _entry_id

        result = _entry_id("   ", "https://example.com", "Title")
        assert result.startswith("h:")

    def test_long_guid_truncated(self):
        from app.services.feeds import _entry_id

        long_guid = "x" * 500
        result = _entry_id(long_guid, "", "")
        assert len(result) <= 200

    def test_all_empty(self):
        from app.services.feeds import _entry_id

        result = _entry_id("", "", "")
        assert result.startswith("h:")
        assert len(result) > 2

    def test_guid_with_leading_trailing_spaces(self):
        from app.services.feeds import _entry_id

        result = _entry_id("  guid-123  ", "", "")
        assert result == "guid-123"


# ─── feeds.new_entries ───────────────────────────────────────────────────── #

class TestNewEntries:
    """Boundaries for feeds.new_entries."""

    def test_none_seen_ids_returns_empty(self):
        from app.services.feeds import Feed, FeedEntry, new_entries

        feed = Feed(title="Test", entries=[
            FeedEntry(entry_id="1", title="Post", link="https://example.com"),
        ])
        assert new_entries(feed, None) == []

    def test_empty_seen_ids_returns_all(self):
        from app.services.feeds import Feed, FeedEntry, new_entries

        entries = [
            FeedEntry(entry_id="1", title="Post", link="https://example.com"),
        ]
        feed = Feed(title="Test", entries=entries)
        assert new_entries(feed, []) == entries

    def test_empty_entry_skipped(self):
        from app.services.feeds import Feed, FeedEntry, new_entries

        feed = Feed(title="Test", entries=[
            FeedEntry(entry_id="1", title="", link="https://example.com"),
        ])
        assert new_entries(feed, []) == []

    def test_all_already_seen(self):
        from app.services.feeds import Feed, FeedEntry, new_entries

        feed = Feed(title="Test", entries=[
            FeedEntry(entry_id="1", title="Post", link="https://example.com"),
        ])
        assert new_entries(feed, ["1"]) == []


# ─── feeds.remember ─────────────────────────────────────────────────────── #

class TestRemember:
    """Boundaries for feeds.remember."""

    def test_none_seen_ids(self):
        from app.services.feeds import Feed, FeedEntry, remember

        feed = Feed(title="T", entries=[
            FeedEntry(entry_id="new1", title="A"),
        ])
        result = remember(None, feed)
        assert result == ["new1"]

    def test_empty_feed_preserves_seen(self):
        from app.services.feeds import Feed, remember

        feed = Feed(title="T", entries=[])
        result = remember(["old1", "old2"], feed)
        assert result == ["old1", "old2"]

    def test_overflow_truncated(self):
        from app.services.feeds import SEEN_IDS_KEPT, Feed, FeedEntry, remember

        entries = [FeedEntry(entry_id=f"e{i}", title=f"T{i}") for i in range(SEEN_IDS_KEPT + 50)]
        feed = Feed(title="T", entries=entries)
        result = remember([], feed)
        assert len(result) == SEEN_IDS_KEPT


# ─── feeds.parse ─────────────────────────────────────────────────────────── #

class TestFeedParse:
    """Boundaries for feeds.parse."""

    def test_empty_xml_raises(self):
        from app.services.feeds import FeedError, parse

        with pytest.raises(FeedError):
            parse("")

    def test_non_feed_xml_raises(self):
        from app.services.feeds import FeedError, parse

        with pytest.raises(FeedError):
            parse("<html><body>Not a feed</body></html>")

    def test_xml_with_entity_declaration_raises(self):
        from app.services.feeds import FeedError, parse

        xml = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE feed [<!ENTITY xxe "bomb">]>'
            '<rss version="2.0"><channel><title>T</title></channel></rss>'
        )
        with pytest.raises(FeedError, match="entity"):
            parse(xml)

    def test_empty_channel(self):
        from app.services.feeds import parse

        feed = parse('<rss version="2.0"><channel><title>Empty</title></channel></rss>')
        assert feed.title == "Empty"
        assert feed.entries == []

    def test_rss_item_no_guid_no_link(self):
        from app.services.feeds import parse

        xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Post</title></item>"
            "</channel></rss>"
        )
        feed = parse(xml)
        assert len(feed.entries) == 1
        assert feed.entries[0].entry_id.startswith("h:")

    def test_atom_feed_minimal(self):
        from app.services.feeds import parse

        xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>Atom</title>"
            "<entry><title>Post</title><id>urn:1</id></entry>"
            "</feed>"
        )
        feed = parse(xml)
        assert feed.title == "Atom"
        assert len(feed.entries) == 1
        assert feed.entries[0].entry_id == "urn:1"


# ─── formatting.clip ────────────────────────────────────────────────────── #

class TestClip:
    """Boundaries for formatting.clip."""

    def test_zero_budget(self):
        from app.services.publishers.formatting import clip

        assert clip("Hello world", 0) == ""

    def test_negative_budget(self):
        from app.services.publishers.formatting import clip

        assert clip("Hello world", -10) == ""

    def test_budget_one(self):
        from app.services.publishers.formatting import clip

        result = clip("Hello world", 1)
        assert len(result) <= 1

    def test_empty_text(self):
        from app.services.publishers.formatting import clip

        assert clip("", 100) == ""

    def test_text_exactly_budget(self):
        from app.services.publishers.formatting import clip

        assert clip("Hello", 5) == "Hello"

    def test_text_with_no_spaces(self):
        from app.services.publishers.formatting import clip

        result = clip("abcdefghij", 5)
        assert len(result) <= 5


# ─── formatting.normalize_tags ───────────────────────────────────────────── #

class TestNormalizeTags:
    """Boundaries for formatting.normalize_tags."""

    def test_empty_list(self):
        from app.services.publishers.formatting import normalize_tags

        assert normalize_tags([], limit=5) == []

    def test_empty_string_tag(self):
        from app.services.publishers.formatting import normalize_tags

        assert normalize_tags([""], limit=5) == []

    def test_all_special_chars_tag(self):
        from app.services.publishers.formatting import normalize_tags

        assert normalize_tags(["!@#$%^&*()"], limit=5) == []

    def test_unicode_stripped(self):
        from app.services.publishers.formatting import normalize_tags

        result = normalize_tags(["café", "naïve"], limit=5)
        assert result == ["caf", "nave"]

    def test_limit_zero(self):
        from app.services.publishers.formatting import normalize_tags

        assert normalize_tags(["python", "rust"], limit=0) == []

    def test_duplicates_deduped(self):
        from app.services.publishers.formatting import normalize_tags

        assert normalize_tags(["Python", "python", "PYTHON"], limit=5) == ["python"]


# ─── formatting.escape_yaml_scalar ───────────────────────────────────────── #

class TestEscapeYamlScalar:
    """Boundaries for formatting.escape_yaml_scalar."""

    def test_empty_string(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar("") == ""

    def test_backslash(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar("C:\\Users") == "C:\\\\Users"

    def test_double_quote(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar('Say "hello"') == 'Say \\"hello\\"'

    def test_newline(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar("line1\nline2") == "line1\\nline2"

    def test_nul_byte(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar("a\x00b") == "a\\x00b"

    def test_c1_control(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar("a\x85b") == "a\\Nb"

    def test_integer_input(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar(42) == "42"

    def test_none_input(self):
        from app.services.publishers.formatting import escape_yaml_scalar

        assert escape_yaml_scalar(None) == "None"


# ─── formatting.front_matter ─────────────────────────────────────────────── #

class TestFrontMatter:
    """Boundaries for formatting.front_matter."""

    def test_empty_dict(self):
        from app.services.publishers.formatting import front_matter

        assert front_matter({}) == "---\n---"

    def test_none_value_skipped(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"title": "Hello", "cover": None})
        assert "cover" not in result

    def test_empty_string_skipped(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"title": "Hello", "slug": ""})
        assert "slug" not in result

    def test_empty_list_skipped(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"tags": []})
        assert "tags" not in result

    def test_boolean_value(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"published": True})
        assert "published: true" in result

    def test_title_with_quotes_escaped(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"title": 'Hello "world"'})
        assert '\\"world\\"' in result

    def test_title_with_backslash_escaped(self):
        from app.services.publishers.formatting import front_matter

        result = front_matter({"title": "C:\\"})
        assert "\\\\" in result


# ─── utm.tag ─────────────────────────────────────────────────────────────── #

class TestUtmTag:
    """Boundaries for utm.tag."""

    def test_none_url(self):
        from app.services.utm import tag

        assert tag(None, source="s", campaign="c") is None

    def test_empty_url(self):
        from app.services.utm import tag

        assert tag("", source="s", campaign="c") == ""

    def test_ftp_url_unchanged(self):
        from app.services.utm import tag

        url = "ftp://example.com/file.zip"
        assert tag(url, source="s", campaign="c") == url

    def test_mailto_unchanged(self):
        from app.services.utm import tag

        url = "mailto:user@example.com"
        assert tag(url, source="s", campaign="c") == url

    def test_existing_utm_not_overwritten(self):
        from app.services.utm import tag

        url = "https://example.com?utm_source=existing"
        result = tag(url, source="new", campaign="c")
        assert "utm_source=existing" in result
        assert "utm_source=new" not in result

    def test_no_netloc(self):
        from app.services.utm import tag

        assert tag("just-a-path", source="s", campaign="c") == "just-a-path"


# ─── utm.host_of ─────────────────────────────────────────────────────────── #

class TestHostOf:
    """Boundaries for utm.host_of."""

    def test_none(self):
        from app.services.utm import host_of

        assert host_of(None) == ""

    def test_empty(self):
        from app.services.utm import host_of

        assert host_of("") == ""

    def test_strips_www(self):
        from app.services.utm import host_of

        assert host_of("https://www.example.com/path") == "example.com"

    def test_strips_port(self):
        from app.services.utm import host_of

        assert host_of("https://example.com:8080/path") == "example.com"

    def test_strips_userinfo(self):
        from app.services.utm import host_of

        assert host_of("https://user:pass@example.com") == "example.com"

    def test_not_a_url(self):
        from app.services.utm import host_of

        assert host_of("not-a-url") == ""


# ─── scheduling.resolve_zone ─────────────────────────────────────────────── #

class TestResolveZone:
    """Boundaries for scheduling.resolve_zone."""

    def test_none(self):
        from app.services.scheduling import resolve_zone

        assert resolve_zone(None) is None

    def test_empty_string(self):
        from app.services.scheduling import resolve_zone

        assert resolve_zone("") is None

    def test_whitespace_only(self):
        from app.services.scheduling import resolve_zone

        assert resolve_zone("   ") is None

    def test_too_long(self):
        from app.services.scheduling import ScheduleError, TIMEZONE_MAX_LENGTH, resolve_zone

        with pytest.raises(ScheduleError):
            resolve_zone("A" * (TIMEZONE_MAX_LENGTH + 1))

    def test_invalid_zone(self):
        from app.services.scheduling import ScheduleError, resolve_zone

        with pytest.raises(ScheduleError):
            resolve_zone("Not/A/Timezone")

    def test_valid_zone(self):
        from zoneinfo import ZoneInfo

        from app.services.scheduling import resolve_zone

        result = resolve_zone("Europe/Berlin")
        assert isinstance(result, ZoneInfo)

    def test_zoneinfo_passthrough(self):
        from zoneinfo import ZoneInfo

        from app.services.scheduling import resolve_zone

        zone = ZoneInfo("UTC")
        assert resolve_zone(zone) is zone


# ─── scheduling.normalize ───────────────────────────────────────────────── #

class TestScheduleNormalize:
    """Boundaries for scheduling.normalize."""

    def test_none_when(self):
        from app.services.scheduling import normalize

        assert normalize(None) is None

    def test_none_when_with_invalid_tz_raises(self):
        from app.services.scheduling import ScheduleError, normalize

        with pytest.raises(ScheduleError):
            normalize(None, tz="BadZone")


# ─── content.word_count_of ───────────────────────────────────────────────── #

class TestWordCountOf:
    """Boundaries for content.word_count_of."""

    def test_empty_string(self):
        from app.models.content import word_count_of

        assert word_count_of("") == 0

    def test_whitespace_only(self):
        from app.models.content import word_count_of

        assert word_count_of("   \n\t  ") == 0

    def test_single_word(self):
        from app.models.content import word_count_of

        assert word_count_of("hello") == 1

    def test_unicode_words(self):
        from app.models.content import word_count_of

        assert word_count_of("日本語 テスト") == 2


# ─── content.read_minutes_for ────────────────────────────────────────────── #

class TestReadMinutesFor:
    """Boundaries for content.read_minutes_for."""

    def test_zero_words(self):
        from app.models.content import read_minutes_for

        assert read_minutes_for(0) == 1

    def test_one_word(self):
        from app.models.content import read_minutes_for

        assert read_minutes_for(1) == 1

    def test_220_words(self):
        from app.models.content import read_minutes_for

        assert read_minutes_for(220) == 1

    def test_221_words(self):
        from app.models.content import read_minutes_for

        assert read_minutes_for(221) == 1

    def test_330_words(self):
        from app.models.content import read_minutes_for

        assert read_minutes_for(330) == 2


# ─── content.clamp_body ─────────────────────────────────────────────────── #

class TestClampBody:
    """Boundaries for content.clamp_body."""

    def test_empty_body(self):
        from app.models.content import clamp_body

        assert clamp_body("") == ""

    def test_body_exactly_at_limit(self):
        from app.models.content import clamp_body

        body = "a" * 100
        assert clamp_body(body, limit=100) == body

    def test_body_one_over_limit(self):
        from app.models.content import clamp_body

        body = "a" * 101
        result = clamp_body(body, limit=100)
        assert len(result) <= 100

    def test_limit_zero(self):
        from app.models.content import clamp_body

        result = clamp_body("hello world", limit=0)
        assert result == ""

    def test_paragraph_break_preferred(self):
        from app.models.content import clamp_body

        body = "First paragraph.\n\nSecond paragraph here."
        result = clamp_body(body, limit=len(body) - 1)
        assert len(result) <= len(body) - 1


# ─── content.clamp_tags ─────────────────────────────────────────────────── #

class TestClampTags:
    """Boundaries for content.clamp_tags."""

    def test_empty_list(self):
        from app.models.content import clamp_tags

        assert clamp_tags([]) == []

    def test_tags_truncated(self):
        from app.models.content import clamp_tags

        tags = ["a" * 200]
        result = clamp_tags(tags, limit=10)
        assert len(result[0]) == 10

    def test_limit_zero_empties_all(self):
        from app.models.content import clamp_tags

        assert clamp_tags(["hello", "world"], limit=0) == []

    def test_whitespace_only_tag_dropped(self):
        from app.models.content import clamp_tags

        assert clamp_tags(["   "]) == []


# ─── rss._clean ──────────────────────────────────────────────────────────── #

class TestRssClean:
    """Boundaries for rss._clean."""

    def test_empty_string(self):
        from app.services.rss import _clean

        assert _clean("") == ""

    def test_none_input(self):
        from app.services.rss import _clean

        assert _clean(None) == ""

    def test_xml_special_chars_escaped(self):
        from app.services.rss import _clean

        result = _clean("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;" in result

    def test_illegal_xml_chars_stripped(self):
        from app.services.rss import _clean

        result = _clean("hello\x00\x01\x02world")
        assert result == "helloworld"

    def test_nul_inside_title(self):
        from app.services.rss import _clean

        assert "\x00" not in _clean("Title\x00Here")


# ─── project.slugify ────────────────────────────────────────────────────── #

class TestSlugify:
    """Boundaries for project.slugify."""

    def test_empty_string(self):
        from app.models.project import slugify

        assert slugify("") == "untitled"

    def test_whitespace_only(self):
        from app.models.project import slugify

        assert slugify("   ") == "untitled"

    def test_all_special_chars(self):
        from app.models.project import slugify

        assert slugify("!!!@@@###") == "untitled"

    def test_unicode_title(self):
        from app.models.project import slugify

        result = slugify("日本語タイトル")
        assert result == "untitled"

    def test_mixed_content(self):
        from app.models.project import slugify

        assert slugify("Hello World!") == "hello-world"

    def test_leading_trailing_hyphens(self):
        from app.models.project import slugify

        assert slugify("---hello---") == "hello"

    def test_consecutive_special_chars(self):
        from app.models.project import slugify

        assert slugify("a!!!b") == "a-b"


# ─── project.scan_due ───────────────────────────────────────────────────── #

class TestScanDue:
    """Boundaries for project.scan_due."""

    def test_never_scanned(self):
        from app.models.project import scan_due

        assert scan_due(None, 24) is True

    def test_no_interval(self):
        from app.models.project import scan_due

        from datetime import UTC, datetime
        assert scan_due(datetime.now(UTC), None) is True

    def test_zero_interval(self):
        from app.models.project import scan_due

        from datetime import UTC, datetime
        assert scan_due(datetime.now(UTC), 0) is True

    def test_negative_interval(self):
        from app.models.project import scan_due

        from datetime import UTC, datetime
        assert scan_due(datetime.now(UTC), -5) is True

    def test_recently_scanned(self):
        from app.models.project import scan_due

        from datetime import UTC, datetime
        assert scan_due(datetime.now(UTC), 24) is False


# ─── feeds._parse_date ──────────────────────────────────────────────────── #

class TestParseDate:
    """Boundaries for feeds._parse_date."""

    def test_empty_string(self):
        from app.services.feeds import _parse_date

        assert _parse_date("") is None

    def test_none(self):
        from app.services.feeds import _parse_date

        assert _parse_date(None) is None

    def test_whitespace_only(self):
        from app.services.feeds import _parse_date

        assert _parse_date("   ") is None

    def test_garbage(self):
        from app.services.feeds import _parse_date

        assert _parse_date("not-a-date") is None

    def test_rfc822(self):
        from app.services.feeds import _parse_date

        result = _parse_date("Mon, 01 Jan 2024 00:00:00 GMT")
        assert result is not None

    def test_iso8601_with_z(self):
        from app.services.feeds import _parse_date

        result = _parse_date("2024-01-01T00:00:00Z")
        assert result is not None

    def test_iso8601_with_offset(self):
        from app.services.feeds import _parse_date

        result = _parse_date("2024-01-01T00:00:00+05:30")
        assert result is not None


# ─── dedup.commit_shas ──────────────────────────────────────────────────── #

class TestCommitShas:
    """Boundaries for dedup.commit_shas."""

    def test_none_activity(self):
        from app.services.dedup import commit_shas

        assert commit_shas(None) == []

    def test_no_commits_attr(self):
        from app.services.dedup import commit_shas

        assert commit_shas(object()) == []

    def test_empty_commits(self):
        from app.services.dedup import commit_shas

        class FakeActivity:
            new_commits = []

        assert commit_shas(FakeActivity()) == []

    def test_sha_truncated_to_seven(self):
        from app.services.dedup import commit_shas

        class FakeCommit:
            sha = "abcdef1234567890"

        class FakeActivity:
            new_commits = [FakeCommit()]

        result = commit_shas(FakeActivity())
        assert result == ["abcdef1"]

    def test_empty_sha_skipped(self):
        from app.services.dedup import commit_shas

        class FakeCommit:
            sha = ""

        class FakeActivity:
            new_commits = [FakeCommit()]

        assert commit_shas(FakeActivity()) == []


# ─── ai.stray_script_runs ───────────────────────────────────────────────── #

class TestStrayScriptRuns:
    """Boundaries for ai.stray_script_runs."""

    def test_empty_text(self):
        from app.services.ai import stray_script_runs

        assert stray_script_runs("") == []

    def test_none_text(self):
        from app.services.ai import stray_script_runs

        assert stray_script_runs(None) == []

    def test_ascii_only(self):
        from app.services.ai import stray_script_runs

        assert stray_script_runs("Hello world, this is a test.") == []

    def test_single_cjk_char_in_english(self):
        from app.services.ai import stray_script_runs

        result = stray_script_runs("The test 漢 framework runs nightly.")
        assert len(result) >= 1

    def test_limit_respected(self):
        from app.services.ai import stray_script_runs

        text = "word 漢 word 字 word 語 word 文 word 句"
        result = stray_script_runs(text, limit=2)
        assert len(result) <= 2


# ─── ai.stray_letter_splices ────────────────────────────────────────────── #

class TestStrayLetterSplices:
    """Boundaries for ai.stray_letter_splices."""

    def test_empty_text(self):
        from app.services.ai import stray_letter_splices

        assert stray_letter_splices("") == []

    def test_none_text(self):
        from app.services.ai import stray_letter_splices

        assert stray_letter_splices(None) == []

    def test_clean_english(self):
        from app.services.ai import stray_letter_splices

        assert stray_letter_splices("The quick brown fox jumps.") == []


# ─── utm.tag_markdown_links ─────────────────────────────────────────────── #

class TestTagMarkdownLinks:
    """Boundaries for utm.tag_markdown_links."""

    def test_empty_body(self):
        from app.services.utm import tag_markdown_links

        assert tag_markdown_links("", host="example.com", source="s", campaign="c") == ""

    def test_no_links(self):
        from app.services.utm import tag_markdown_links

        body = "Just plain text, no links here."
        assert tag_markdown_links(body, host="example.com", source="s", campaign="c") == body

    def test_link_inside_code_fence_untouched(self):
        from app.services.utm import tag_markdown_links

        body = "```\n[link](https://example.com/path)\n```"
        result = tag_markdown_links(body, host="example.com", source="s", campaign="c")
        assert "utm_source" not in result

    def test_different_host_untouched(self):
        from app.services.utm import tag_markdown_links

        body = "[link](https://other.com/path)"
        result = tag_markdown_links(body, host="example.com", source="s", campaign="c")
        assert "utm_source" not in result

    def test_matching_host_tagged(self):
        from app.services.utm import tag_markdown_links

        body = "[link](https://example.com/path)"
        result = tag_markdown_links(body, host="example.com", source="s", campaign="c")
        assert "utm_source=s" in result


# ─── seo._keyword_density ───────────────────────────────────────────────── #

class TestKeywordDensity:
    """Boundaries for seo._keyword_density."""

    def test_empty_text(self):
        from app.services.seo import _keyword_density

        assert _keyword_density("", "test") == 0.0

    def test_empty_keyword(self):
        from app.services.seo import _keyword_density

        assert _keyword_density("Some text here.", "") == 0.0

    def test_keyword_not_present(self):
        from app.services.seo import _keyword_density

        assert _keyword_density("The quick brown fox.", "zebra") == 0.0

    def test_keyword_present(self):
        from app.services.seo import _keyword_density

        result = _keyword_density("test the test again test", "test")
        assert result > 0.0
