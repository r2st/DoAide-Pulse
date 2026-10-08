"""Does every link in this post actually go anywhere?

Free models invent plausible documentation URLs. Not malformed ones — well-formed
links to pages that have never existed, `https://fastapi.tiangolo.com/advanced/
custom-middleware/`, the sort of thing you only catch by clicking. A 404 in a
published post is the most visible failure mode Pulse has, and it costs a few
HTTP requests to rule out.

The verdict space is deliberately three-valued, and that is the whole design:

``ok``
    The server answered 2xx or 3xx. The page is there.
``broken``
    The server said, definitively, that it is not: 404 or 410. Or the URL points
    somewhere no reader can follow — a loopback or private address.
``unknown``
    Everything else. A timeout, a DNS failure, a 403 from a bot-blocker, a 401
    behind a paywall, a 429, a 500. None of these is evidence about whether the
    page exists.

Only ``broken`` is allowed to stop a publish. Treating ``unknown`` as broken
would mean a flaky network, an aggressive Cloudflare rule, or a publish from a
host with no outbound access blocks the post — and the failure would look
identical to a genuinely dead link, which is the worst of both. A checker that
cries wolf gets switched off, and then it catches nothing at all.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.errors import friendly_network_error

logger = logging.getLogger(__name__)

OK = "ok"
BROKEN = "broken"
UNKNOWN = "unknown"

#: A server saying the resource is gone. The only statuses we treat as proof.
_DEFINITELY_GONE = {404, 410}

_CODE_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
#: ``[label](url "title")`` and its image form. The URL is everything up to
#: whitespace or the closing paren, so a title never lands in the URL.
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_BARE_URL = re.compile(r"https?://[^\s<>\]\)\"'`]+")

#: Punctuation that is far more likely to be the sentence's than the URL's.
_TRAILING_JUNK = ".,;:!?'\"”’)]}>"


@dataclass(frozen=True)
class LinkStatus:
    """One URL and what we found out about it."""

    url: str
    #: :data:`OK`, :data:`BROKEN` or :data:`UNKNOWN`.
    status: str
    http_status: int | None = None
    #: Reader-facing explanation. Always says what to do about it.
    detail: str = ""

    @property
    def is_broken(self) -> bool:
        """Definitively dead. :data:`UNKNOWN` is not broken — see the module docstring."""
        return self.status == BROKEN


def extract_urls(body_markdown: str, *, limit: int | None = None) -> list[str]:
    """Every http(s) URL a reader could click, in the order they appear.

    Code is stripped first. A URL inside a fence or backticks is usually
    illustrative — ``curl https://api.example.com/v1/things`` — and HEAD-checking
    a sample endpoint proves nothing while making the check slower and noisier.

    Relative links, anchors and ``mailto:`` are out of scope: there is no request
    that would settle them.
    """
    text = _INLINE_CODE.sub(" ", _CODE_FENCE.sub(" ", body_markdown or ""))

    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_MD_LINK, _AUTOLINK, _BARE_URL):
        for match in pattern.finditer(text):
            url = (match.group(1) if pattern is not _BARE_URL else match.group(0)).strip()
            url = url.rstrip(_TRAILING_JUNK)
            if not url.lower().startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)

    return found[:limit] if limit else found


def _unreachable_for_a_reader(url: str) -> str | None:
    """Why a reader could never follow this URL, or ``None`` if they could.

    Also the reason nothing below resolves a hostname twice: the check runs on
    the server, so a link to ``localhost`` or ``10.0.0.5`` would otherwise turn
    the checker into a probe of Pulse's own network. Refusing those is both the
    correct editorial verdict — the link is broken for everyone who is not on
    this box — and the right thing to do with a URL a language model wrote.

    **One private address is enough to refuse the host.** A name is allowed to
    carry several A/AAAA records, and nothing says the client will pick the same
    one this function looked at: ``evil.example`` publishing ``93.184.216.34``
    *and* ``169.254.169.254`` is a two-line zone file, and a check that only
    refused when *every* address was private would wave it through and leave
    httpx to choose. So the verdict is taken over any address, not all of them.
    A legitimate split-horizon name loses its link check to this; a metadata
    endpoint reached by a webhook does not lose anything, because it was never
    supposed to be reachable.
    """
    host = (urlsplit(url).hostname or "").strip()
    if not host:
        return "No hostname — this link cannot resolve."

    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        # DNS failure is not proof: a transient resolver problem looks the same
        # as a domain that does not exist. Left for the request to settle.
        return None

    private = sorted(address for address in addresses if _is_private(address))
    if private:
        return (
            f"Resolves to a private or loopback address ({host} → "
            f"{', '.join(private)}) — nobody outside this machine can open it."
        )
    return None


def unreachable_reason(url: str) -> str | None:
    """Why *url* points somewhere no outside caller could reach, or ``None``.

    The same pre-flight the link checker runs, exposed for the other places
    Pulse is handed a URL by a user or a model and then asks the server to open
    it — outbound webhooks above all. Resolving the host and refusing loopback,
    private and link-local addresses is what stops "call this URL when a post
    goes out" from being a request to read the cloud metadata endpoint.
    """
    return _unreachable_for_a_reader(url)


def _is_private(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - getaddrinfo returns valid addresses
        return False
    return (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_unspecified
    )


_MAX_REDIRECTS = 10


def _status_only(url: str, *, client: httpx.Client, method: str) -> httpx.Response:
    """One request, body abandoned unread.

    Returned from inside the ``with`` on purpose: leaving the block closes the
    stream, and everything this module reads off a response — the status code,
    the ``Location`` header, the request it was made from — is still there
    afterwards. What is gone is the body, which is the point.
    """
    with client.stream(method, url) as response:
        return response


def _follow_safely(
    url: str, *, client: httpx.Client, method: str = "HEAD"
) -> httpx.Response:
    """Follow redirects manually, checking each hop for SSRF.

    httpx's built-in ``follow_redirects`` resolves DNS independently from
    ``_unreachable_for_a_reader``, so a 302 to ``http://169.254.169.254/``
    would bypass the pre-flight check. Following by hand lets us validate
    every Location header before the client connects.

    Every hop is **streamed and closed unread**. A verdict here is built from a
    status code and a ``Location`` header and nothing else, but ``client.request``
    buffers the entire body first to hand back a ``.text`` this function never
    looks at. On a HEAD that costs nothing; the GET retry below is the problem,
    and it is not a hypothetical one — the retry exists precisely for hosts that
    refuse HEAD, so the bodies Pulse ends up fetching are exactly the ones it
    did not choose to. The URL comes out of a user's document, the response size
    is decided by whoever owns it, and :func:`check` runs
    ``link_check_max_urls`` of these at once inside a request somebody is
    waiting on: a handful of links to large files is a memory spike with no cap
    on it at all. :func:`app.services.feeds._read_capped` has the same problem
    and caps it; here the cap can be zero, because the body was never wanted.
    """
    current = url
    for _ in range(_MAX_REDIRECTS):
        response = _status_only(current, client=client, method=method)
        if response.is_redirect:
            location = response.headers.get("location", "")
            if not location:
                return response
            # Resolve relative → absolute via httpx's URL handling.
            resolved = str(response.next_request.url) if response.next_request else location
            ssrf = _unreachable_for_a_reader(resolved)
            if ssrf:
                raise _SSRFRedirect(resolved, ssrf)
            current = resolved
            continue
        return response
    # Exceeded redirect budget — treat as unreachable.
    raise httpx.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects", request=response.request)


class _SSRFRedirect(Exception):
    """A redirect tried to reach a private address."""

    def __init__(self, target: str, reason: str) -> None:
        self.target = target
        self.reason = reason
        super().__init__(f"Redirect to {target}: {reason}")


def check_url(url: str, *, client: httpx.Client) -> LinkStatus:
    """Resolve one URL to a verdict. Never raises."""
    unreachable = _unreachable_for_a_reader(url)
    if unreachable:
        logger.warning("link check: SSRF-blocked url=%s reason=%s", url, unreachable)
        return LinkStatus(url, BROKEN, detail=unreachable)

    try:
        response = _follow_safely(url, client=client, method="HEAD")
        # A great many servers do not implement HEAD, and answer 405 or 403 to
        # it while serving GET perfectly well. Retrying with GET is the
        # difference between a useful checker and one that flags half the web.
        if response.status_code in (403, 405, 501):
            response = _follow_safely(url, client=client, method="GET")
    except _SSRFRedirect as exc:
        return LinkStatus(
            url,
            BROKEN,
            detail=f"Redirects to a private address ({exc.target}) — {exc.reason}",
        )
    except httpx.HTTPError as exc:
        return LinkStatus(
            url,
            UNKNOWN,
            detail=f"{friendly_network_error(exc)} — check it by hand.",
        )

    code = response.status_code
    if code < 400:
        return LinkStatus(url, OK, http_status=code)
    if code in _DEFINITELY_GONE:
        return LinkStatus(
            url,
            BROKEN,
            http_status=code,
            detail=f"Returns {code} — this page does not exist. Fix or remove the link.",
        )
    return LinkStatus(
        url,
        UNKNOWN,
        http_status=code,
        detail=(
            f"Returned {code}, which is not proof either way (a bot block, a "
            "paywall or a bad day). Check it by hand."
        ),
    )


def check(urls: list[str], *, timeout: float | None = None) -> list[LinkStatus]:
    """Verdicts for *urls*, in the order given.

    Concurrent because this runs inside a publish request: 25 links checked one
    after another at a 10-second timeout is four minutes of somebody waiting,
    and the requests are entirely independent.
    """
    if not urls:
        return []

    budget = timeout if timeout is not None else settings.link_check_timeout_seconds
    with (
        httpx.Client(
            timeout=budget,
            follow_redirects=False,
            headers={
                # Some hosts 403 an unidentified client outright. Saying who we
                # are turns a pile of `unknown` verdicts into real answers.
                "User-Agent": "Pulse/0.1 link-checker (+https://github.com/r2st/DoAide-Pulse)",
                "Accept": "*/*",
            },
        ) as client,
        ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool,
    ):
        results = list(pool.map(lambda url: check_url(url, client=client), urls))

    n_broken = sum(1 for r in results if r.status == BROKEN)
    n_unknown = sum(1 for r in results if r.status == UNKNOWN)
    if n_broken:
        logger.warning(
            "link check: %d checked, %d broken, %d unknown",
            len(results),
            n_broken,
            n_unknown,
        )
    elif n_unknown:
        logger.info(
            "link check: %d checked, %d unknown", len(results), n_unknown
        )
    else:
        logger.debug("link check: %d checked, all ok", len(results))
    return results


def check_body(
    body_markdown: str, *, extra_urls: list[str] | None = None
) -> list[LinkStatus]:
    """Check every link in a post body, plus any *extra_urls* (a cover image).

    Capped at ``link_check_max_urls``. The cap is a latency guard, not a policy:
    a link-farm of a post is unusual, and 25 requests is already a second or two.

    The cap falls on the *body's* links, never on the caller's. Trimming the
    combined list put the extras last and therefore first over the edge: a post
    with the cap's worth of links in it silently stopped having its cover image
    checked, and the cover is the one URL every reader sees and nobody clicks —
    a dead one is a broken card in every feed, which is exactly what
    ``app.routers.content._check_content_links`` passes it here to catch.
    """
    cap = settings.link_check_max_urls
    # Deduped in order, so a cover image that is also linked in the body does
    # not spend two of the budget.
    extras = [url for url in dict.fromkeys(extra_urls or []) if url][:cap]
    kept = [url for url in extract_urls(body_markdown) if url not in extras][
        : max(0, cap - len(extras))
    ]
    return check(kept + extras)


def broken(statuses: list[LinkStatus]) -> list[LinkStatus]:
    """Just the ones that are definitively dead."""
    return [s for s in statuses if s.is_broken]


__all__ = [
    "BROKEN",
    "OK",
    "UNKNOWN",
    "LinkStatus",
    "broken",
    "check",
    "check_body",
    "check_url",
    "extract_urls",
    "unreachable_reason",
]
