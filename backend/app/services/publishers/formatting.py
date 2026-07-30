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

#: Mastodon's default. Instances can raise it — `configuration.statuses.
#: max_characters` on `/api/v1/instance` — and many do, but none of the ones
#: worth posting to lower it, so assuming the floor is always safe and costs an
#: extra request to discover otherwise. Like Twitter, links count as a fixed 23.
MASTODON_LIMIT = 500
MASTODON_LINK_COST = 23

#: Bluesky counts graphemes and does *not* shorten links, so a long URL really
#: does eat a third of the post.
BLUESKY_LIMIT = 300


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


def lead_image_html(url: str, *, alt: str = "") -> str:
    """A cover image as the post's first block of HTML.

    For the platforms with no cover-image field of their own. Medium and
    WordPress both take the *first image in the body* as the preview, so putting
    one there is the only way to set it — Medium's API exposes no cover
    parameter at all, and WordPress's featured image is a media **id**, which
    means uploading the bytes first rather than handing over a URL.

    The alt text is the title, which is a genuine description of a cover image
    and better than the empty string a decorative-image argument would justify.
    """
    if not url:
        return ""
    return f'<figure><img src="{escape_attribute(url)}" alt="{escape_attribute(alt)}" /></figure>'


def escape_attribute(value: str) -> str:
    """Escape a string for use inside a double-quoted HTML attribute."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


def clip(text: str, budget: int) -> str:
    """*text* collapsed to one line and cut to *budget* characters.

    Prefers a word boundary, falling back to a hard cut for text with no spaces
    in reach — a long identifier, or CJK, where every position is a boundary and
    ``rfind(" ")`` finds nothing.
    """
    body = re.sub(r"\s+", " ", text).strip()
    if len(body) <= budget:
        return body
    clipped = body[: budget - 1]
    cut = clipped.rfind(" ")
    return (clipped[:cut] if cut > budget // 2 else clipped).rstrip(",;:.") + "…"


def truncate_with_link(
    text: str, *, url: str | None = None, limit: int, url_cost: int | None = None
) -> str:
    """Fit *text* plus an optional trailing link inside *limit* characters.

    The link is appended after truncation and reserved for beforehand, so a long
    permalink never pushes the post over. *url_cost* is what the platform
    charges for a link regardless of its real length — 23 on Twitter and
    Mastodon, which rewrite them — and defaults to counting the characters,
    which is what platforms like Bluesky actually do.
    """
    if not url:
        return clip(text, limit)
    reserve = (len(url) if url_cost is None else url_cost) + 1
    return f"{clip(text, limit - reserve)} {url}"


def truncate_for_tweet(text: str, *, url: str | None = None) -> str:
    """Fit *text* (plus an optional trailing link) inside one tweet."""
    return truncate_with_link(text, url=url, limit=TWEET_LIMIT, url_cost=TCO_LENGTH)


def compose_social(
    *,
    text: str,
    url: str | None,
    tags: list[str],
    limit: int,
    url_cost: int | None = None,
    tag_limit: int = 3,
) -> str:
    """One short post: a hook, the link, and a hashtag line.

    Shared by the platforms that get a single bounded post rather than an
    article — Mastodon and Bluesky. The link closes the hook and the hashtags
    sit on their own line, and both are budgeted for *before* the hook is
    trimmed: a post that drops its link to make room for prose has lost the only
    part of itself that does any work.
    """
    hashtags = hashtagify(tags, limit=tag_limit)
    # Every trailing block costs its own length plus the blank line above it.
    reserve = len(hashtags) + 2 if hashtags else 0
    hook = truncate_with_link(
        text, url=url, limit=limit - reserve, url_cost=url_cost
    )
    return f"{hook}\n\n{hashtags}" if hashtags else hook


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
    "BLUESKY_LIMIT",
    "LINKEDIN_HARD_LIMIT",
    "LINKEDIN_SOFT_LIMIT",
    "MASTODON_LIMIT",
    "MASTODON_LINK_COST",
    "TCO_LENGTH",
    "TWEET_LIMIT",
    "clip",
    "compose_social",
    "escape_attribute",
    "front_matter",
    "hashtagify",
    "lead_image_html",
    "normalize_tags",
    "to_html",
    "to_plain_text",
    "truncate_for_linkedin",
    "truncate_for_tweet",
    "truncate_with_link",
]
