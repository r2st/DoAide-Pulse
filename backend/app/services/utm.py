"""Campaign tagging for the links Pulse publishes.

Pulse can see how a post did *on the platform* only where the platform has a
stats API, which today is Dev.to and nowhere else. What it can always see —
through whatever analytics the project's own site already runs — is the traffic
that arrives from a post. Tagging the outbound link is what makes that arrival
attributable, so "views on Dev.to" becomes "visits to the product, from Dev.to".

Two rules shape everything here:

* **A ``rel=canonical`` URL is never tagged.** A canonical must be the exact
  address of the original; a tagged variant declares a *different* page as the
  original and does more damage than the attribution is worth. So the tagging
  applies to the share link and to the project's own URL, and
  ``PublishRequest.canonical_url`` stays byte-for-byte what was published.
* **An explicit parameter wins.** If a URL already carries ``utm_source`` —
  because somebody pasted a link they had already tagged — that value is left
  alone rather than overwritten with a guess.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: The parameters this module manages. Anything else on the URL is preserved.
_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")

#: A Markdown inline link target: the ``(…)`` half of ``[text](url "title")``.
#:
#: The URL alternation allows a balanced ``(…)`` inside the address, which
#: CommonMark does and which real URLs use — ``/wiki/Ruby_(programming)``,
#: ``/docs/api_(v2)``. Reading the address as "anything up to the first ``)``"
#: stopped one character early and then took the article's ``)`` as the link's
#: own, so a rewrite published ``…/api_(v2?utm_source=devto)`` — a dead link,
#: on the user's own domain, in prose Pulse had been asked only to tag.
#:
#: One level of nesting, not arbitrary depth, which a regex cannot do. Anything
#: deeper or unbalanced simply fails to match and is left exactly as written —
#: the right outcome either way, since not tagging a link costs some attribution
#: and mangling one costs the reader the page.
_MD_LINK = re.compile(
    r"\]\(\s*(?P<url>https?://(?:[^\s()]|\([^\s()]*\))+)"
    r"(?P<title>\s+\"[^\"]*\")?\s*\)"
)

#: A fenced code block, so the rewriter can leave its contents alone.
_FENCE = re.compile(r"(^|\n)(?P<fence>```|~~~)[^\n]*\n.*?(?:\n(?P=fence)|\Z)", re.S)


def tag(
    url: str | None,
    *,
    source: str,
    campaign: str,
    medium: str = "referral",
    content: str = "",
) -> str | None:
    """*url* with UTM parameters added, or ``None``/``""`` unchanged.

    Only ``http`` and ``https`` URLs are touched. Anything else — a mailto:, a
    bare path, a malformed string a user typed — is returned as it came in,
    because appending a query string to it is at best useless and at worst
    breaks a link that would otherwise have worked.
    """
    if not url:
        return url

    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return url

    wanted = {
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign,
        "utm_content": content,
    }

    # keep_blank_values, so a deliberate `?utm_source=` still counts as present
    # and is not quietly filled in.
    existing = parse_qsl(parts.query, keep_blank_values=True)
    present = {key for key, _ in existing}

    added = [
        (key, wanted[key])
        for key in _KEYS
        if wanted.get(key) and key not in present
    ]
    if not added:
        return url

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(existing + added),
            parts.fragment,
        )
    )


def host_of(url: str | None) -> str:
    """The lowercase hostname of *url*, ``www.`` stripped, or ``""``."""
    if not url:
        return ""
    netloc = urlsplit(url.strip()).netloc.lower()
    host = netloc.rpartition("@")[2].partition(":")[0]
    return host[4:] if host.startswith("www.") else host


def tag_markdown_links(body: str, *, host: str, **params: str) -> str:
    """Campaign-tag the Markdown links in *body* that point at *host*.

    This is where the attribution actually comes from. A Dev.to article is read
    on Dev.to and Pulse never sees a click; what the project's own analytics
    sees is a visit arriving from the link inside that article. Tagging the
    outbound ``project_url`` alone would not touch it, because the blogging
    adapters put the body on the page and nothing else.

    Deliberately narrow, because rewriting somebody's prose is not something to
    do approximately:

    * only ``[text](url)`` targets — never bare URLs in running text, which
      might be quoted rather than linked;
    * only links whose host matches the project's own, so third-party
      references and documentation links are untouched;
    * never inside a fenced code block, where a URL is an example, not a link.
    """
    if not body or not host:
        return body

    def rewrite(chunk: str) -> str:
        def replace(match: re.Match[str]) -> str:
            url = match["url"]
            if host_of(url) != host:
                return match[0]
            return f"]({tag(url, **params)}{match['title'] or ''})"

        return _MD_LINK.sub(replace, chunk)

    out: list[str] = []
    cursor = 0
    for fence in _FENCE.finditer(body):
        out.append(rewrite(body[cursor : fence.start()]))
        out.append(fence[0])
        cursor = fence.end()
    out.append(rewrite(body[cursor:]))
    return "".join(out)


__all__ = ["host_of", "tag", "tag_markdown_links"]
