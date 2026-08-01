"""What a post looks like when somebody pastes its link into a feed.

Herald already gets the post onto the platform. This module is about the *other*
place every post appears: the unfurled card in a timeline, a Slack channel or a
LinkedIn share. That card is assembled by the crawler from Open Graph and
Twitter Card meta tags, and when they are missing the network invents something
— usually the first eighty characters of navigation chrome, over a blank grey
rectangle. The post is fine; the thing 90% of people see is not.

Two jobs, deliberately kept apart:

* :func:`meta_tags` produces the tags themselves, for the one destination where
  Herald controls the page — a Git-published blog. Everywhere else the platform
  writes its own head and ignores anything we send.

* :func:`previews` and :func:`audit` answer "what will this look like, and what
  is wrong with it", for the editor. Neither fetches anything: they are pure
  functions of the content fields, so they are cheap enough to run on every
  keystroke and honest about being predictions.

**On the numbers below.** Every truncation limit here is observed rendering
behaviour, not a published contract. Networks retune them, and a card is laid
out by viewport as well as by character count, so these say "this is roughly
where it clips" — enough to stop you shipping a title that dies mid-word, not
enough to promise a pixel. The image *requirements*, by contrast, are documented
by the platforms and are treated as facts.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.services.seo import strip_markdown, truncate_at_sentence

#: The one image size that satisfies every network at once: 1.91:1 is
#: Facebook's documented recommendation, and at 1200x630 it also clears
#: Twitter's `summary_large_image` minimum of 300x157 with room to spare.
#: Recommending a single size is the whole point — a user who has to pick per
#: network picks none.
RECOMMENDED_IMAGE = (1200, 630)

#: Twitter's documented floor for `summary_large_image`. Below it the card
#: silently degrades to the small square `summary` layout.
TWITTER_MIN_IMAGE = (300, 157)

#: Roughly where each network stops showing the title in a feed. See the module
#: docstring: observed, not promised.
_TITLE_CLIP = {
    "x": 70,
    "linkedin": 119,
    "facebook": 88,
    "slack": 75,
}

#: Same, for the description line under the title. Slack and LinkedIn are the
#: generous ones; X often shows none at all on mobile, and the number here is
#: the desktop case.
_DESCRIPTION_CLIP = {
    "x": 125,
    "linkedin": 110,
    "facebook": 155,
    "slack": 190,
}

#: Display names, so the frontend does not carry a second copy of this mapping.
NETWORK_LABELS = {
    "x": "X / Twitter",
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "slack": "Slack",
}

NETWORKS = tuple(NETWORK_LABELS)

#: An absolute http(s) URL. Crawlers do not resolve relative paths — they are
#: fetching from their own servers, where the post's origin is not implied — so
#: a relative cover image is a blank card, every time.
_ABSOLUTE_URL = re.compile(r"^https?://", re.I)

#: Extensions no crawler will render as a card image. SVG is the interesting
#: one: browsers show it happily, and every major unfurler refuses it.
_BAD_IMAGE_EXT = (".svg", ".webp", ".avif", ".bmp", ".tiff", ".ico")


def _clean(text: str) -> str:
    """Collapse a field to the single line a card actually renders.

    Markdown is stripped because a title carrying ``**bold**`` shows the
    asterisks in the unfurl, and newlines are collapsed because a card has no
    line breaks to give.
    """
    return " ".join(strip_markdown(text or "").split())


def clip(text: str, limit: int) -> str:
    """Truncate to *limit*, on a word boundary, with a real ellipsis.

    Mirrors what the networks do rather than what :func:`str` does: they cut at
    a word and append a single-character ellipsis, so a title that fits exactly
    is shown whole and one that does not loses a word rather than half of one.
    """
    text = _clean(text)
    if len(text) <= limit:
        return text
    # -1 for the ellipsis the network appends inside the same budget.
    cut = text[: max(0, limit - 1)]
    spaced = cut.rsplit(" ", 1)[0]
    # A single word longer than the budget has no boundary to fall back on.
    return f"{spaced or cut}…"


def domain_of(url: str) -> str:
    """The bare domain a card shows above the title, or ``""``.

    ``www.`` is dropped because the networks drop it, and the whole point of
    this function is to render what they render.
    """
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _card_description(meta_description: str, excerpt: str, body_markdown: str) -> str:
    """The description a crawler would end up with, in the order it is tried.

    Falls all the way back to the body so the preview shows *something* — a
    crawler with no ``og:description`` does exactly this, and seeing the raw
    first sentence in the panel is how the user learns the meta description is
    doing nothing.
    """
    for candidate in (meta_description, excerpt):
        cleaned = _clean(candidate)
        if cleaned:
            return cleaned
    return truncate_at_sentence(_clean(body_markdown), 200)


# --------------------------------------------------------------------------- #
# The tags themselves                                                          #
# --------------------------------------------------------------------------- #


def meta_tags(
    *,
    title: str,
    url: str,
    meta_description: str = "",
    excerpt: str = "",
    body_markdown: str = "",
    cover_image_url: str | None = None,
    site_name: str = "",
    author_handle: str = "",
    published_at: str | None = None,
    tags: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Open Graph and Twitter Card tags, as ``(attribute, name, content)`` pairs.

    Returned as an ordered list of ``(name, content)`` rather than a dict
    because ``article:tag`` legitimately repeats — one tag per keyword — and a
    dict would keep only the last one.

    ``og:title`` is *not* clipped to the feed limits. Those limits are about
    display; the tag should carry the real title and let each network cut it
    where it likes. Clipping here would mean every network showed the shortest
    network's version.
    """
    description = _card_description(meta_description, excerpt, body_markdown)
    clean_title = _clean(title)

    tags_out: list[tuple[str, str]] = [
        ("og:type", "article"),
        ("og:title", clean_title),
        ("og:description", description),
    ]
    if url:
        tags_out.append(("og:url", url))
    if site_name:
        tags_out.append(("og:site_name", site_name))

    if cover_image_url:
        tags_out += [
            ("og:image", cover_image_url),
            # Dimensions let a crawler lay the card out before the image
            # finishes downloading, which is the difference between a card that
            # appears instantly and one that pops in. We publish the
            # recommended size because that is the size we tell users to supply;
            # a mismatch costs nothing but the pre-layout.
            ("og:image:width", str(RECOMMENDED_IMAGE[0])),
            ("og:image:height", str(RECOMMENDED_IMAGE[1])),
            # Empty alt is the correct signal for a decorative card image, and
            # an omitted one makes some crawlers reuse the title as alt text.
            ("og:image:alt", clean_title),
            ("twitter:card", "summary_large_image"),
            ("twitter:image", cover_image_url),
        ]
    else:
        # Without an image the large card renders as a title over dead space.
        # The small card is the honest fallback and looks deliberate.
        tags_out.append(("twitter:card", "summary"))

    tags_out += [
        ("twitter:title", clean_title),
        ("twitter:description", description),
    ]
    if author_handle:
        handle = author_handle if author_handle.startswith("@") else f"@{author_handle}"
        tags_out += [("twitter:creator", handle), ("twitter:site", handle)]
    if published_at:
        tags_out.append(("article:published_time", published_at))
    for tag in tags or []:
        tags_out.append(("article:tag", tag))

    return tags_out


#: Twitter's tags are ``name=``; Open Graph's are ``property=``. Crawlers are
#: lenient about this in practice, but the validators are not, and a user
#: pasting our output into their own template deserves the correct form.
def _attribute_for(key: str) -> str:
    return "name" if key.startswith("twitter:") else "property"


def render_meta_tags(tags: list[tuple[str, str]]) -> str:
    """The tags as an HTML block, ready to paste into a ``<head>``.

    Everything is escaped: a title containing a quote would otherwise close the
    attribute and turn the rest of the tag into markup.
    """
    return "\n".join(
        f'<meta {_attribute_for(key)}="{html.escape(key, quote=True)}" '
        f'content="{html.escape(value, quote=True)}">'
        for key, value in tags
    )


def front_matter_keys(tags: list[tuple[str, str]]) -> dict[str, str]:
    """The subset of tags worth writing into a static site's front matter.

    Meta tags cannot be appended to a Markdown body the way JSON-LD can: a
    crawler reads ``<head>``, and a body-level ``<meta>`` is ignored by every
    one of them. So the Git publisher passes these as front-matter keys instead
    and lets the theme put them in the head, which is the only place they work.

    Camel-cased because that is what Astro, Next and Eleventy themes expect.
    """
    wanted = {
        "og:title": "ogTitle",
        "og:description": "ogDescription",
        "og:image": "ogImage",
        "twitter:card": "twitterCard",
    }
    lookup = dict(tags)
    return {key: lookup[tag] for tag, key in wanted.items() if lookup.get(tag)}


# --------------------------------------------------------------------------- #
# What it will look like                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Preview:
    """One network's rendering of the card, as far as it can be predicted."""

    network: str
    label: str
    #: Title and description already clipped to this network's limit.
    title: str
    description: str
    #: True when clipping actually removed something — the editor highlights
    #: these, because a full title is the one thing the user can fix.
    title_clipped: bool
    description_clipped: bool
    domain: str
    image_url: str | None
    #: "summary_large_image" or "summary" — the layout, which changes the whole
    #: shape of the card and is decided entirely by whether an image exists.
    card_type: str

    def as_dict(self) -> dict:
        return {
            "network": self.network,
            "label": self.label,
            "title": self.title,
            "description": self.description,
            "title_clipped": self.title_clipped,
            "description_clipped": self.description_clipped,
            "domain": self.domain,
            "image_url": self.image_url,
            "card_type": self.card_type,
        }


def previews(
    *,
    title: str,
    url: str = "",
    meta_description: str = "",
    excerpt: str = "",
    body_markdown: str = "",
    cover_image_url: str | None = None,
) -> list[Preview]:
    """One :class:`Preview` per network, in a stable order."""
    description = _card_description(meta_description, excerpt, body_markdown)
    clean_title = _clean(title)
    domain = domain_of(url)
    # A relative URL is not a card image — see _ABSOLUTE_URL. Showing it in the
    # preview would promise an image the crawler will never fetch.
    image = cover_image_url if cover_image_url and _ABSOLUTE_URL.match(cover_image_url) else None

    out = []
    for network in NETWORKS:
        title_limit = _TITLE_CLIP[network]
        description_limit = _DESCRIPTION_CLIP[network]
        out.append(
            Preview(
                network=network,
                label=NETWORK_LABELS[network],
                title=clip(clean_title, title_limit),
                description=clip(description, description_limit),
                title_clipped=len(clean_title) > title_limit,
                description_clipped=len(description) > description_limit,
                domain=domain,
                image_url=image,
                card_type="summary_large_image" if image else "summary",
            )
        )
    return out


@dataclass(frozen=True)
class CardIssue:
    """One thing that will visibly degrade the card."""

    #: "error" means the card is broken or blank; "warn" means it renders but
    #: worse than it could. Same vocabulary as :class:`app.services.seo.SeoIssue`
    #: so the editor can style both lists identically.
    level: str
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "field": self.field, "message": self.message}


def audit(
    *,
    title: str,
    meta_description: str = "",
    excerpt: str = "",
    body_markdown: str = "",
    cover_image_url: str | None = None,
) -> list[CardIssue]:
    """Everything that will make the unfurled card worse, worst first.

    Only checks what can be known without a network call. Whether the image URL
    actually resolves is a different question, answered by the link checker and
    by the editor's live thumbnail — both of which make the real request.
    """
    issues: list[CardIssue] = []
    clean_title = _clean(title)
    description = _card_description(meta_description, excerpt, body_markdown)

    if not clean_title:
        issues.append(CardIssue("error", "title", "No title — the card will show the URL."))

    cover = (cover_image_url or "").strip()
    if not cover:
        issues.append(
            CardIssue(
                "error",
                "cover_image_url",
                "No cover image. The link unfurls as a small text-only card — "
                f"add one at {RECOMMENDED_IMAGE[0]}×{RECOMMENDED_IMAGE[1]} for the "
                "large layout on every network.",
            )
        )
    elif not _ABSOLUTE_URL.match(cover):
        issues.append(
            CardIssue(
                "error",
                "cover_image_url",
                "The cover image is a relative path. Crawlers fetch from their "
                "own servers and cannot resolve it — use an absolute https:// URL.",
            )
        )
    elif cover.lower().split("?")[0].endswith(_BAD_IMAGE_EXT):
        suffix = cover.lower().split("?")[0].rsplit(".", 1)[-1]
        issues.append(
            CardIssue(
                "warn",
                "cover_image_url",
                f"A .{suffix} cover is not rendered by most unfurlers. "
                "Use PNG or JPEG.",
            )
        )

    if not description:
        issues.append(
            CardIssue(
                "warn",
                "meta_description",
                "No description — the card shows the title alone.",
            )
        )
    elif len(description) < 50:
        issues.append(
            CardIssue(
                "warn",
                "meta_description",
                f"The description is {len(description)} characters. Under about "
                "50 the card looks unfinished on LinkedIn and Slack.",
            )
        )

    # Reported against the *tightest* network rather than each in turn: four
    # near-identical warnings for one long title is the kind of panel people
    # stop reading.
    tightest_title = min(_TITLE_CLIP, key=lambda n: _TITLE_CLIP[n])
    limit = _TITLE_CLIP[tightest_title]
    if len(clean_title) > limit:
        issues.append(
            CardIssue(
                "warn",
                "title",
                f"The title is {len(clean_title)} characters and clips at about "
                f"{limit} on {NETWORK_LABELS[tightest_title]}.",
            )
        )

    order = {"error": 0, "warn": 1}
    return sorted(issues, key=lambda i: order.get(i.level, 2))


def summary(
    *,
    title: str,
    url: str = "",
    meta_description: str = "",
    excerpt: str = "",
    body_markdown: str = "",
    cover_image_url: str | None = None,
    site_name: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Everything the social panel needs, in one response."""
    built = meta_tags(
        title=title,
        url=url,
        meta_description=meta_description,
        excerpt=excerpt,
        body_markdown=body_markdown,
        cover_image_url=cover_image_url,
        site_name=site_name,
        tags=tags,
    )
    return {
        "previews": [
            p.as_dict()
            for p in previews(
                title=title,
                url=url,
                meta_description=meta_description,
                excerpt=excerpt,
                body_markdown=body_markdown,
                cover_image_url=cover_image_url,
            )
        ],
        "issues": [
            i.as_dict()
            for i in audit(
                title=title,
                meta_description=meta_description,
                excerpt=excerpt,
                body_markdown=body_markdown,
                cover_image_url=cover_image_url,
            )
        ],
        "meta_tags": [{"key": key, "value": value} for key, value in built],
        "meta_html": render_meta_tags(built),
        "recommended_image": {
            "width": RECOMMENDED_IMAGE[0],
            "height": RECOMMENDED_IMAGE[1],
        },
    }


__all__ = [
    "NETWORKS",
    "NETWORK_LABELS",
    "RECOMMENDED_IMAGE",
    "TWITTER_MIN_IMAGE",
    "CardIssue",
    "Preview",
    "audit",
    "clip",
    "domain_of",
    "front_matter_keys",
    "meta_tags",
    "previews",
    "render_meta_tags",
    "summary",
]
