"""Markdown → whatever each platform actually accepts.

Content is authored once in Markdown. Platforms disagree about almost
everything downstream of that: Dev.to wants Markdown with YAML front matter,
Medium and WordPress want HTML, LinkedIn wants plain text with no markup at all,
and Twitter wants plain text under 280 characters. These helpers are the whole
of that translation layer, kept out of the adapters so the character-counting
edge cases are testable without a network.
"""
from __future__ import annotations

import re

import markdown as markdown_lib
from bs4 import BeautifulSoup

#: Extensions that cover what generated posts actually use: fenced code blocks
#: with language hints, tables, and footnote-free smart handling of line breaks.
_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

#: Twitter's limit. URLs are counted as 23 characters regardless of length
#: (t.co wrapping), which is why the reserve below is a constant, not len(url).
TWEET_LIMIT = 280
TCO_LENGTH = 23

#: LinkedIn truncates a post behind "…see more" around here. Not a hard limit
#: (that is 3000), but the point past which nobody reads.
LINKEDIN_SOFT_LIMIT = 1300
LINKEDIN_HARD_LIMIT = 3000


def to_html(body_markdown: str) -> str:
    """Render Markdown to the HTML subset every blogging platform accepts."""
    return markdown_lib.markdown(body_markdown, extensions=_MD_EXTENSIONS)


def to_plain_text(body_markdown: str) -> str:
    """Markdown reduced to readable plain text, paragraphs preserved.

    Goes via HTML rather than stripping syntax with regexes so that lists,
    tables and code blocks degrade to something a human can still read.
    """
    soup = BeautifulSoup(to_html(body_markdown), "html.parser")

    # Block elements need a real break or the text runs together into one wall.
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre"]):
        tag.append("\n\n")
    for tag in soup.find_all("br"):
        tag.replace_with("\n")

    text = soup.get_text()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def front_matter(fields: dict[str, object]) -> str:
    """A YAML front-matter block, for platforms that take one (Dev.to).

    Hand-rolled rather than a YAML dependency: the value space here is strings,
    booleans and flat string lists, and a real serializer would still need the
    same quoting decisions made explicitly.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (list, tuple)):
            rendered = ", ".join(f'"{str(v)}"' for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            # Double quotes inside a double-quoted scalar must be escaped.
            escaped = str(value).replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
    lines.append("---")
    return "\n".join(lines)


def truncate_for_tweet(text: str, *, url: str | None = None) -> str:
    """Fit *text* (plus an optional trailing link) inside one tweet.

    The URL is appended after truncation and reserved for at its t.co length, so
    a long permalink never pushes the post over the limit.
    """
    reserve = (TCO_LENGTH + 1) if url else 0
    budget = TWEET_LIMIT - reserve

    body = re.sub(r"\s+", " ", text).strip()
    if len(body) > budget:
        clipped = body[: budget - 1]
        # Prefer a word boundary; fall back to a hard cut for text with no
        # spaces (a long identifier, CJK).
        cut = clipped.rfind(" ")
        body = (clipped[:cut] if cut > budget // 2 else clipped).rstrip(",;:.") + "…"

    return f"{body} {url}" if url else body


def truncate_for_linkedin(text: str, *, url: str | None = None) -> str:
    """Trim to LinkedIn's hard limit, keeping the link on its own line.

    No attempt is made to stay under the "…see more" fold — that is an
    editorial choice, and cutting a post to 1300 characters to avoid it loses
    more than the fold costs.
    """
    reserve = len(url) + 2 if url else 0
    budget = LINKEDIN_HARD_LIMIT - reserve

    body = text.strip()
    if len(body) > budget:
        clipped = body[: budget - 1]
        cut = clipped.rfind(" ")
        body = (clipped[:cut] if cut > budget // 2 else clipped).rstrip() + "…"

    return f"{body}\n\n{url}" if url else body


def normalize_tags(tags: list[str], *, limit: int, allow_spaces: bool = False) -> list[str]:
    """Platform-safe tags: alphanumeric, lowercase, deduped, capped.

    Dev.to rejects the whole post for a tag with a hyphen in it, and Medium
    silently drops anything past the fifth, so this is enforcement rather than
    tidying.
    """
    pattern = r"[^a-z0-9 ]+" if allow_spaces else r"[^a-z0-9]+"
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = re.sub(pattern, "", str(tag).lower()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out[:limit]


def hashtagify(tags: list[str], *, limit: int = 3) -> str:
    """Tags as a trailing hashtag line for the social platforms."""
    cleaned = normalize_tags(tags, limit=limit)
    return " ".join(f"#{t}" for t in cleaned)


__all__ = [
    "LINKEDIN_HARD_LIMIT",
    "LINKEDIN_SOFT_LIMIT",
    "TCO_LENGTH",
    "TWEET_LIMIT",
    "front_matter",
    "hashtagify",
    "normalize_tags",
    "to_html",
    "to_plain_text",
    "truncate_for_linkedin",
    "truncate_for_tweet",
]
