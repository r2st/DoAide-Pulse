"""SEO hygiene applied to generated content — deterministic, not another LLM call.

The model is asked for a meta description and keywords as part of the generation
envelope, but what it returns needs enforcing: descriptions run long, keywords
come back as a wall of near-duplicates, and headers get skipped so a post opens
at H3. All of that is mechanical, so it is done here rather than spent as a
second round-trip.

:func:`audit` is the same rules read out loud, for the editor's SEO panel.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

#: Google truncates meta descriptions around 155-160 characters. We aim under
#: the lower bound so a trailing word is never clipped mid-thought.
META_DESCRIPTION_MAX = 155
META_DESCRIPTION_MIN = 70

#: Titles beyond ~60 chars get an ellipsis in results pages.
TITLE_MAX = 60

#: More than this and the keywords stop being a focus and start being a list.
KEYWORD_MAX = 8

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_MARKUP = re.compile(r"[*_`>\[\]()#]")


def _find_headings(body_markdown: str) -> list[tuple[str, str]]:
    """Find markdown headings, ignoring those inside fenced code blocks.

    Without stripping fences first, a Python comment like ``# import os``
    inside a code block is falsely detected as an H1.
    """
    return _HEADING.findall(_CODE_FENCE.sub(" ", body_markdown or ""))


def strip_markdown(markdown: str) -> str:
    """Body text with fences, markup and link syntax removed.

    Used for excerpting and word counts; not a general-purpose renderer.
    """
    text = _CODE_FENCE.sub(" ", markdown)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> label
    text = _INLINE_MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_at_sentence(text: str, limit: int) -> str:
    """Trim *text* to *limit* chars, preferring a sentence boundary.

    Falls back to a word boundary with an ellipsis, so the result never ends
    mid-word — a clipped meta description reads as broken, not as truncated.
    """
    text = text.strip()
    if len(text) <= limit:
        return text

    kept: list[str] = []
    length = 0
    for sentence in _SENTENCE_END.split(text):
        if length + len(sentence) + 1 > limit:
            break
        kept.append(sentence)
        length += len(sentence) + 1
    if kept:
        return " ".join(kept).strip()

    clipped = text[: limit - 1]
    return clipped[: clipped.rfind(" ")].rstrip(",;:") + "…"


def build_meta_description(candidate: str, *, fallback_body: str) -> str:
    """A meta description of the right length, from the model's or the body."""
    text = strip_markdown(candidate).strip() or strip_markdown(fallback_body)
    return truncate_at_sentence(text, META_DESCRIPTION_MAX)


def normalize_keywords(keywords: list[str], *, extra: list[str] | None = None) -> list[str]:
    """Lowercase, dedupe, drop noise, and cap at :data:`KEYWORD_MAX`.

    Order is preserved because the model's ordering is roughly its own
    relevance ranking, and the first keyword is the one that ends up in the
    title check.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in [*keywords, *(extra or [])]:
        keyword = re.sub(r"\s+", " ", str(raw)).strip().lower().strip("#,.")
        # Single characters and bare numbers are never a useful keyword.
        if len(keyword) < 2 or keyword.isdigit() or keyword in seen:
            continue
        seen.add(keyword)
        out.append(keyword)
    return out[:KEYWORD_MAX]


def build_excerpt(body_markdown: str, *, limit: int = 220) -> str:
    """The first real paragraph, trimmed — used as the social blurb."""
    for block in body_markdown.split("\n\n"):
        text = strip_markdown(block)
        # Skip the title line and any leading image/badge row.
        if len(text) > 40 and not block.lstrip().startswith("#"):
            return truncate_at_sentence(text, limit)
    return truncate_at_sentence(strip_markdown(body_markdown), limit)


@dataclass(frozen=True)
class SeoIssue:
    """One thing to fix, and how bad it is."""

    #: "warn" is advisory; "error" means a platform or a crawler will visibly
    #: mishandle the post.
    level: str
    field: str
    message: str


#: Default minimum SEO score for auto-publishing. Content scoring below this
#: stays in review instead of being auto-published.
SEO_SCORE_THRESHOLD = 70

#: Image alt text pattern.
_IMG_ALT = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def _keyword_density(plain_text: str, keyword: str) -> float:
    """Percentage of *keyword* occurrences relative to total words."""
    words = plain_text.split()
    if not words or not keyword:
        return 0.0
    count = plain_text.lower().count(keyword.lower())
    return (count / len(words)) * 100


def _first_paragraph(body_markdown: str) -> str:
    """Extract the first real paragraph (not a heading or image)."""
    for block in body_markdown.split("\n\n"):
        text = strip_markdown(block).strip()
        if len(text) > 30 and not block.lstrip().startswith("#"):
            return text
    return ""


def audit(
    *,
    title: str,
    body_markdown: str,
    meta_description: str,
    keywords: list[str],
    cover_image_url: str | None = None,
    focus_keyword: str = "",
    slug: str = "",
) -> list[SeoIssue]:
    """Everything wrong with this piece's SEO, worst first.

    Returns an empty list for a clean piece. The editor shows this verbatim, so
    every message says what to do, not just what is wrong.
    """
    issues: list[SeoIssue] = []

    if not title.strip():
        issues.append(SeoIssue("error", "title", "The post has no title."))
    elif len(title) > TITLE_MAX:
        issues.append(
            SeoIssue(
                "warn",
                "title",
                f"Title is {len(title)} characters — search results cut off around "
                f"{TITLE_MAX}. Trim the tail.",
            )
        )

    if not meta_description.strip():
        issues.append(
            SeoIssue("error", "meta_description", "No meta description — search "
                     "engines will invent one from the body.")
        )
    elif len(meta_description) < META_DESCRIPTION_MIN:
        issues.append(
            SeoIssue(
                "warn",
                "meta_description",
                f"Meta description is only {len(meta_description)} characters. "
                f"Aim for {META_DESCRIPTION_MIN}–{META_DESCRIPTION_MAX}.",
            )
        )

    # Use focus_keyword if provided, else fall back to first keyword.
    fk = (focus_keyword or "").strip()
    if not fk and keywords:
        fk = keywords[0]

    if not keywords:
        issues.append(
            SeoIssue("warn", "keywords", "No keywords set — nothing to optimise for.")
        )
    elif fk and title:
        if fk.lower() not in title.lower():
            issues.append(
                SeoIssue(
                    "warn",
                    "title",
                    f'Focus keyword "{fk}" does not appear in the title.',
                )
            )

    # Focus keyword checks.
    plain = strip_markdown(body_markdown)
    if fk:
        # Keyword density.
        density = _keyword_density(plain, fk)
        if density < 0.5:
            issues.append(
                SeoIssue(
                    "warn",
                    "body",
                    f'Focus keyword "{fk}" density is {density:.1f}% — aim for 1-2%.',
                )
            )
        elif density > 3.0:
            issues.append(
                SeoIssue(
                    "warn",
                    "body",
                    f'Focus keyword "{fk}" density is {density:.1f}% — that reads '
                    "as stuffing. Aim for 1-2%.",
                )
            )

        # First paragraph.
        first_para = _first_paragraph(body_markdown)
        if first_para and fk.lower() not in first_para.lower():
            issues.append(
                SeoIssue(
                    "warn",
                    "body",
                    f'Focus keyword "{fk}" missing from the opening paragraph.',
                )
            )

        # Subheadings.
        headings = _find_headings(body_markdown)
        heading_texts = [text for _, text in headings]
        if heading_texts and not any(fk.lower() in h.lower() for h in heading_texts):
            issues.append(
                SeoIssue(
                    "warn",
                    "body",
                    f'Focus keyword "{fk}" not in any subheading. Include it in '
                    "at least one H2 or H3.",
                )
            )

        # Meta description.
        if meta_description and fk.lower() not in meta_description.lower():
            issues.append(
                SeoIssue(
                    "warn",
                    "meta_description",
                    f'Focus keyword "{fk}" missing from the meta description.',
                )
            )

        # Slug.
        if slug:
            slug_words = slug.lower().replace("-", " ")
            if fk.lower() not in slug_words:
                issues.append(
                    SeoIssue(
                        "warn",
                        "slug",
                        f'Focus keyword "{fk}" not in the URL slug.',
                    )
                )

    headings = _find_headings(body_markdown)
    levels = [len(hashes) for hashes, _ in headings]
    if not levels:
        issues.append(
            SeoIssue(
                "warn",
                "body",
                "No headings — long posts without H2s are hard to scan and rank.",
            )
        )
    else:
        # A jump from H2 straight to H4 breaks the document outline. Only the
        # *first* such jump is reported: after one, the rest are noise.
        previous = levels[0]
        for level in levels[1:]:
            if level > previous + 1:
                issues.append(
                    SeoIssue(
                        "warn",
                        "body",
                        f"Heading levels skip from H{previous} to H{level}. "
                        "Keep the outline contiguous.",
                    )
                )
                break
            previous = level

    if not (cover_image_url or "").strip():
        issues.append(
            SeoIssue(
                "warn",
                "cover_image_url",
                "No cover image. Dev.to, Medium and Hashnode all show one in "
                "their feeds, and it is what LinkedIn and Twitter use for the "
                "link preview — a post without one is a wall of text in every "
                "list it appears in.",
            )
        )

    # Image alt text: images without alt text hurt accessibility and SEO.
    images = _IMG_ALT.findall(body_markdown)
    empty_alts = sum(1 for alt in images if not alt.strip())
    if empty_alts:
        issues.append(
            SeoIssue(
                "warn",
                "body",
                f"{empty_alts} image(s) missing alt text. Add descriptive alts "
                "for accessibility and SEO.",
            )
        )

    words = len(plain.split())
    if words < 300:
        issues.append(
            SeoIssue(
                "warn",
                "body",
                f"Only {words} words. Posts under 300 rarely rank for anything.",
            )
        )

    # Slug length check.
    if slug and len(slug) > 60:
        issues.append(
            SeoIssue(
                "warn",
                "slug",
                f"Slug is {len(slug)} characters — keep it under 60 for search "
                "result display.",
            )
        )

    # Errors first so the editor's panel leads with what actually breaks.
    return sorted(issues, key=lambda i: 0 if i.level == "error" else 1)


def seo_score(
    *,
    title: str,
    body_markdown: str,
    meta_description: str,
    keywords: list[str],
    cover_image_url: str | None = None,
    focus_keyword: str = "",
    slug: str = "",
) -> int:
    """A 0–100 SEO score derived from the audit checks.

    Each check contributes points to a weighted total. A perfect post scores
    100; the autopilot uses :data:`SEO_SCORE_THRESHOLD` (default 70) to decide
    whether to auto-publish.
    """
    score = 100  # Start perfect, deduct for each issue.

    fk = (focus_keyword or "").strip()
    if not fk and keywords:
        fk = keywords[0]

    # Title checks (15 points).
    if not title.strip():
        score -= 15
    elif len(title) > TITLE_MAX:
        score -= 5

    # Meta description (15 points).
    if not meta_description.strip():
        score -= 15
    elif len(meta_description) < META_DESCRIPTION_MIN:
        score -= 5
    elif fk and fk.lower() not in meta_description.lower():
        score -= 5

    # Keywords presence (5 points).
    if not keywords:
        score -= 5

    # Focus keyword in title (10 points).
    if fk and title and fk.lower() not in title.lower():
        score -= 10

    plain = strip_markdown(body_markdown)
    words = plain.split()

    # Keyword density (10 points).
    if fk:
        density = _keyword_density(plain, fk)
        if density < 0.5 or density > 3.0:
            score -= 10
        elif density < 1.0 or density > 2.5:
            score -= 5

    # First paragraph (10 points).
    if fk:
        first_para = _first_paragraph(body_markdown)
        if first_para and fk.lower() not in first_para.lower():
            score -= 10

    # Subheadings (10 points).
    headings = _find_headings(body_markdown)
    heading_texts = [text for _, text in headings]
    if not heading_texts:
        score -= 10
    elif fk and not any(fk.lower() in h.lower() for h in heading_texts):
        score -= 5

    # Heading hierarchy (5 points).
    levels = [len(hashes) for hashes, _ in headings]
    if levels:
        previous = levels[0]
        for level in levels[1:]:
            if level > previous + 1:
                score -= 5
                break
            previous = level

    # Cover image (5 points).
    if not (cover_image_url or "").strip():
        score -= 5

    # Image alt text (5 points).
    images = _IMG_ALT.findall(body_markdown)
    if any(not alt.strip() for alt in images):
        score -= 5

    # Word count (5 points).
    if len(words) < 300:
        score -= 5

    # Slug (5 points).
    if slug:
        if len(slug) > 60:
            score -= 3
        if fk and fk.lower() not in slug.lower().replace("-", " "):
            score -= 2

    return max(0, score)


# ── Phase 2: Structured Data ─────────────────────────────────────────── #


def build_json_ld(
    *,
    title: str,
    body_markdown: str,
    meta_description: str,
    url: str,
    cover_image_url: str | None = None,
    published_at: str | None = None,
    modified_at: str | None = None,
    author_name: str = "",
    publisher_name: str = "",
    publisher_logo_url: str = "",
    keywords: list[str] | None = None,
) -> str:
    """Return a JSON-LD ``Article`` block ready to embed in a page.

    Produces valid https://schema.org/Article markup. For GIT destinations
    this is appended as a ``<script type="application/ld+json">`` block at
    the end of the Markdown; for WordPress it can be injected via post meta.
    """
    plain = strip_markdown(body_markdown)
    word_count = len(plain.split())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    schema: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title[:110],  # schema.org recommends ≤110
        "description": meta_description or truncate_at_sentence(plain, META_DESCRIPTION_MAX),
        "wordCount": word_count,
        "datePublished": published_at or now_iso,
    }
    if modified_at:
        schema["dateModified"] = modified_at
    if url:
        schema["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    if cover_image_url:
        schema["image"] = cover_image_url
    if keywords:
        schema["keywords"] = keywords
    if author_name:
        schema["author"] = {"@type": "Person", "name": author_name}
    if publisher_name:
        pub: dict[str, object] = {"@type": "Organization", "name": publisher_name}
        if publisher_logo_url:
            pub["logo"] = {"@type": "ImageObject", "url": publisher_logo_url}
        schema["publisher"] = pub

    return json.dumps(schema, indent=2, ensure_ascii=False)


def json_ld_script_tag(json_ld: str) -> str:
    """Wrap a JSON-LD string in a ``<script>`` element for Markdown embedding."""
    return f'\n<script type="application/ld+json">\n{json_ld}\n</script>\n'


# ── Phase 2: Sitemap Generation ──────────────────────────────────────── #


def build_sitemap_entry(
    *,
    url: str,
    lastmod: str | None = None,
    changefreq: str = "weekly",
    priority: str = "0.7",
) -> Element:
    """Build a single ``<url>`` element for a sitemap."""
    url_el = Element("url")
    loc = SubElement(url_el, "loc")
    loc.text = url
    if lastmod:
        mod = SubElement(url_el, "lastmod")
        mod.text = lastmod
    freq = SubElement(url_el, "changefreq")
    freq.text = changefreq
    prio = SubElement(url_el, "priority")
    prio.text = priority
    return url_el


def build_sitemap_xml(entries: list[dict[str, str]]) -> str:
    """Build a complete ``sitemap.xml`` from a list of entry dicts.

    Each dict has ``url`` (required), ``lastmod``, ``changefreq``, ``priority``.
    Returns a UTF-8 XML string.
    """
    urlset = Element("urlset")
    urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")

    for entry in entries:
        url_el = build_sitemap_entry(
            url=entry["url"],
            lastmod=entry.get("lastmod"),
            changefreq=entry.get("changefreq", "weekly"),
            priority=entry.get("priority", "0.7"),
        )
        urlset.append(url_el)

    xml_bytes = tostring(urlset, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_bytes}\n'


__all__ = [
    "KEYWORD_MAX",
    "META_DESCRIPTION_MAX",
    "META_DESCRIPTION_MIN",
    "SEO_SCORE_THRESHOLD",
    "TITLE_MAX",
    "SeoIssue",
    "audit",
    "build_excerpt",
    "build_json_ld",
    "build_meta_description",
    "build_sitemap_entry",
    "build_sitemap_xml",
    "json_ld_script_tag",
    "normalize_keywords",
    "seo_score",
    "strip_markdown",
    "truncate_at_sentence",
]
