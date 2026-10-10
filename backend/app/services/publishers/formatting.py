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
import nh3
from bs4 import BeautifulSoup

#: Extensions that cover what generated posts actually use: fenced code blocks
#: with language hints, tables, and footnote-free smart handling of line breaks.
_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

#: The tags a rendered Pulse post is allowed to contain — the union of what
#: ``_MD_EXTENSIONS`` can emit and what :func:`lead_image_html` prepends.
#: Anything outside this list is dropped by :func:`sanitize_html`.
_ALLOWED_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "ins", "sup", "sub", "small",
    "code", "pre", "kbd", "samp", "var",
    "blockquote", "q", "cite",
    "ul", "ol", "li", "dl", "dt", "dd",
    "a", "img", "figure", "figcaption",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "span", "div",
}  # fmt: skip

#: Per-tag attribute allow-list. ``class`` is permitted on the code elements
#: because ``fenced_code`` puts the language hint there (``class="language-py"``)
#: and every platform's highlighter reads it; dropping it would silently turn
#: every code block on a published post into unhighlighted grey.
_ALLOWED_ATTRIBUTES = {
    # No ``rel``: ``link_rel`` below manages it, and nh3 refuses to be given
    # both.
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
    "th": {"align", "colspan", "rowspan", "scope"},
    "td": {"align", "colspan", "rowspan"},
    "col": {"align", "span"},
    "ol": {"start"},
}

#: Schemes a link or image may point at. ``javascript:`` is the omission that
#: matters; ``data:`` is left out too, since a data URL is how an image
#: smuggles a script past a reviewer who only looked at the tag name.
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def sanitize_html(html: str) -> str:
    """Strip anything executable out of rendered post HTML.

    Markdown is *not* a sanitiser and has never claimed to be: Python-Markdown
    passes raw HTML in the source straight through to its output, by design.
    That is fine when the author and the reader are the same person, and it is
    not what happens here — a Pulse post reaches its audience on the author's
    Medium or WordPress blog, and the Markdown it was rendered from may have
    been written by a model, assembled from an RSS trigger, or pasted in from
    somewhere nobody vetted. Any of those paths can carry a ``<script>`` tag, an
    ``onerror=`` handler or a ``javascript:`` href to every reader the author
    has.

    Both target platforms sanitise their own input, which is exactly the
    argument for doing it here as well rather than instead: the one that stops
    mattering is whichever one is silently relaxed first, and Pulse finds out
    about that from its readers.

    An allow-list rather than a block-list, and ``nh3`` (Rust's ammonia) rather
    than a hand-rolled pass over the soup, because the interesting failures in
    this area are not the tags anyone thinks to ban — they are mutation-XSS,
    where a parser's error recovery reassembles harmless-looking markup into a
    tag that was never written.
    """
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        strip_comments=True,
        # Outbound links on a published post point wherever the body said, so
        # they get the usual treatment for links you do not control.
        link_rel="noopener noreferrer",
    )

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
    """Render Markdown to the HTML subset every blogging platform accepts.

    Sanitised on the way out — see :func:`sanitize_html` for why the renderer's
    raw-HTML passthrough is not something to hand to a publisher unfiltered.
    """
    return sanitize_html(markdown_lib.markdown(body_markdown, extensions=_MD_EXTENSIONS))


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
    # Entity decoding can resurrect angle-bracket sequences that look like
    # tags: markdown entity-encodes `<iframe` to `&lt;iframe`, nh3 leaves
    # the entity alone because it is not a tag, and get_text() decodes it
    # back to `<iframe`.  The result is text, not markup — but a social
    # platform that renders HTML would treat it as one.  Strip anything
    # that survived the round trip.
    text = re.sub(r"<[^>]*>", "", text)
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


#: The characters a double-quoted YAML scalar cannot carry literally, and what
#: YAML spells them as. The backslash has to be in here — escaping the quote
#: without it is what turns a title ending in one (``C:\``) into ``"C:\"``, an
#: unterminated scalar that fails the whole document.
_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\x85": "\\N",
    "\u2028": "\\L",
    "\u2029": "\\P",
}


def escape_yaml_scalar(value: object) -> str:
    """Escape *value* for use inside a double-quoted YAML scalar.

    Walks the string once rather than chaining ``str.replace``: a chain has to
    escape the backslash first and then never touch the backslashes it just
    wrote, which is a rule that holds right up until someone adds a case in the
    wrong order. A single pass cannot double-escape.

    Three things are being prevented, and only the first is cosmetic:

    * **Corruption.** ``\\n`` typed literally in a title is a YAML escape, so an
      unescaped backslash turns ``Use \\n to break lines`` into a real newline,
      and ``\\\\d+`` in a regex into ``\\d+``.
    * **A document that will not parse.** A trailing backslash escapes the
      closing quote, and a raw quote inside a flow sequence ends the item early.
      Either one fails the static-site build this file is committed to.
    * **Front-matter injection.** The block is delimited by ``---`` lines, so a
      newline in a value could close it early and spill the rest of that value
      into the document as keys — or into the body. Values here are titles,
      descriptions and keywords: model-written text, committed to the user's
      blog repo. None of it is trusted enough to go in raw.
    """
    out: list[str] = []
    for char in str(value):
        escape = _YAML_ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < "\x20" or char == "\x7f":
            # C0 and DEL are outside YAML's printable set.
            out.append(f"\\x{ord(char):02x}")
        elif "\x80" <= char <= "\x9f":
            # C1 likewise.
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def _quoted(value: object) -> str:
    """*value* as a double-quoted YAML scalar, safe to drop into a document."""
    return f'"{escape_yaml_scalar(value)}"'


def front_matter(fields: dict[str, object]) -> str:
    """A YAML front-matter block, for the platforms that take one.

    Hand-rolled rather than a YAML dependency: the value space here is strings,
    booleans and flat string lists, and a real serializer would still need the
    same quoting decisions made explicitly. What it must not skip is the
    escaping — see :func:`escape_yaml_scalar` for what goes wrong without it.
    Keys are Pulse's own constants and are never escaped; values are not.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (list, tuple)):
            # Escaped item by item. A flow sequence is not a place a raw quote
            # is any safer than it is in a plain scalar — it ends the item
            # there and the rest of the document is read as syntax.
            lines.append(f"{key}: [{', '.join(_quoted(v) for v in value)}]")
        else:
            lines.append(f"{key}: {_quoted(value)}")
    lines.append("---")
    return "\n".join(lines)


def clip(text: str, budget: int) -> str:
    """*text* collapsed to one line and cut to *budget* characters.

    Prefers a word boundary, falling back to a hard cut for text with no spaces
    in reach — a long identifier, or CJK, where every position is a boundary and
    ``rfind(" ")`` finds nothing.

    A budget of zero or less returns nothing. That case is not hypothetical: it
    is what a caller hands over once the link and the hashtag line have eaten
    the whole post, and the obvious slice (``body[:budget - 1]``) quietly
    returns *almost the entire string* for a non-positive budget — which is how
    a 300-character Bluesky post came out at 470 and was rejected. Empty is the
    only answer that fits.
    """
    body = re.sub(r"\s+", " ", text).strip()
    if budget <= 0:
        return ""
    if len(body) <= budget:
        return body
    clipped = body[: budget - 1]
    cut = clipped.rfind(" ")
    return (clipped[:cut] if cut > budget // 2 else clipped).rstrip(",;:.") + "…"


def billed_length(text: str, *, url: str | None = None, url_cost: int | None = None) -> int:
    """How long *text* is by the platform's own accounting.

    Twitter and Mastodon rewrite a link and charge a flat rate for it however
    long it really is, so ``len()`` is the wrong ruler there — it is the right
    one on Bluesky, which charges what the URL actually costs. This is the ruler
    the composers check themselves against.
    """
    if not url or url_cost is None or url not in text:
        return len(text)
    return len(text) - len(url) + url_cost


def truncate_with_link(
    text: str, *, url: str | None = None, limit: int, url_cost: int | None = None
) -> str:
    """Fit *text* plus an optional trailing link inside *limit* characters.

    The link is appended after truncation and reserved for beforehand, so a long
    permalink never pushes the post over. *url_cost* is what the platform
    charges for a link regardless of its real length — 23 on Twitter and
    Mastodon, which rewrite them — and defaults to counting the characters,
    which is what platforms like Bluesky actually do.

    The hook is what gives way when the two will not both fit, all the way to
    nothing: a post that dropped its link to make room for prose has lost the
    only part of itself that does any work. The one case this cannot rescue is
    a URL longer than the whole limit, where the post goes out as the bare link
    and over budget — a truncated URL is a dead link, so there is no better
    answer here. Pulse's own share links stay well clear of that; see the
    ``utm_content`` cap in ``app.services.publishing_service``.
    """
    if not url:
        return clip(text, limit)
    cost = len(url) if url_cost is None else url_cost
    hook = clip(text, limit - cost - 1)
    return f"{hook} {url}" if hook else url


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

    There is a third thing that can give way, and it gives way last: the
    hashtags themselves. A campaign-tagged share link runs to a couple of
    hundred characters, and on Bluesky — which shortens nothing — the link plus
    a hashtag line can leave no room for a hook at all. Dropping the hashtags
    is the one cut that does not change what the post is *for*, so the result
    is checked against the platform's own ruler and recomposed without them
    rather than going out over the limit and being rejected.
    """
    hashtags = hashtagify(tags, limit=tag_limit)
    if hashtags:
        # Every trailing block costs its own length plus the blank line above it.
        reserve = len(hashtags) + 2
        hook = truncate_with_link(
            text, url=url, limit=limit - reserve, url_cost=url_cost
        )
        post = f"{hook}\n\n{hashtags}"
        if billed_length(post, url=url, url_cost=url_cost) <= limit:
            return post
    return truncate_with_link(text, url=url, limit=limit, url_cost=url_cost)


def social_budget(
    *,
    text: str,
    url: str | None,
    tags: list[str],
    url_cost: int | None = None,
    tag_limit: int = 3,
) -> int:
    """How long :func:`compose_social` would be *if nothing were cut*.

    The composer always returns something inside the limit, so its output
    cannot tell you whether it had to trim. This assembles the same post from
    the same parts and measures it by the same ruler, without the trimming —
    so ``social_budget(...) > limit`` is exactly "this piece will be shortened
    to fit", and the difference is by how much.

    For preflight only. Nothing publishes this string; it is measured and
    thrown away. It mirrors :func:`compose_social`'s assembly (hook, then the
    link a space later, then the hashtags after a blank line) rather than
    calling it, because what is wanted here is the length the composer *could
    not* have.
    """
    hashtags = hashtagify(tags, limit=tag_limit)
    post = f"{text} {url}" if url else text
    if hashtags:
        post = f"{post}\n\n{hashtags}"
    return billed_length(post, url=url, url_cost=url_cost)


def truncate_for_linkedin(text: str, *, url: str | None = None) -> str:
    """Trim to LinkedIn's hard limit, keeping the link on its own line.

    No attempt is made to stay under the "…see more" fold — that is an
    editorial choice, and cutting a post to 1300 characters to avoid it loses
    more than the fold costs.

    An empty *text* yields the bare link rather than a link with two blank lines
    in front of it. The separator exists to hold the link away from the prose,
    and with no prose there is nothing to hold it away from — the same call
    :func:`truncate_with_link` already makes for the short platforms, made here
    too so the two do not disagree about the same empty string.
    """
    reserve = len(url) + 2 if url else 0
    budget = LINKEDIN_HARD_LIMIT - reserve

    body = text.strip()
    if len(body) > budget:
        clipped = body[: budget - 1]
        cut = clipped.rfind(" ")
        body = (clipped[:cut] if cut > budget // 2 else clipped).rstrip() + "…"

    if not url:
        return body
    return f"{body}\n\n{url}" if body else url


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
    "billed_length",
    "clip",
    "compose_social",
    "escape_attribute",
    "escape_yaml_scalar",
    "front_matter",
    "hashtagify",
    "lead_image_html",
    "normalize_tags",
    "sanitize_html",
    "social_budget",
    "to_html",
    "to_plain_text",
    "truncate_for_linkedin",
    "truncate_for_tweet",
    "truncate_with_link",
]
