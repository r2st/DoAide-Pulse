"""The contract every publishing adapter implements.

An adapter is stateless: it takes a :class:`PublishRequest` and a credential
dict and returns a :class:`PublishResult`. It never touches the database, which
is what lets the publish task own retries, status transitions and error
recording in one place regardless of platform.

Failures are split into two kinds because the caller's response differs:

* :class:`CredentialError` — the token is wrong, expired or lacks a scope.
  Retrying is pointless; the connection is marked invalid and the user is asked
  to reconnect.
* :class:`RateLimited` — the platform declined to process the request and said
  when to come back. Retried, but not until then.
* :class:`PublishError` — anything else. Might be transient, so it is retried
  with backoff up to ``PUBLISH_MAX_RETRIES``.

There are two layers of retry, and they answer different questions. Inside
:meth:`Adapter._request` a handful of fast in-process retries paper over the
blips that resolve in seconds — a dropped connection, a 503 from a load
balancer rolling. Above it, the publication row is re-armed by the beat sweep
for anything that outlives them. The inner layer only retries what is *safe* to
retry: see :func:`_is_retryable`, which is deliberately conservative about
POSTs, because a duplicate article is worse than a failed one, and which hands a
long ``Retry-After`` upwards rather than sleeping through a fraction of it.

**Three adapters are pointed at a host the user typed.** WordPress, Mastodon and
Bluesky are all "tell me where your server is" platforms, so their base URL
arrives from a settings form and Pulse's own process is what opens it —
``http://169.254.169.254/`` is a valid site URL, and the reply comes back to the
caller inside the error message. Those adapters set
:attr:`Adapter.user_supplied_host`, which makes :meth:`Adapter._request` resolve
the host and refuse loopback, private and link-local space before connecting,
and follow redirects by hand so a public host cannot bounce the request inside
the network. It is the same guard :mod:`app.services.link_check` applies to
outbound webhooks, for the same reason.
"""
from __future__ import annotations

import email.utils
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.models.publication import Platform
from app.services import link_check
from app.services.errors import friendly_network_error

logger = logging.getLogger(__name__)

#: Methods that can be replayed without changing the outcome. A retried POST
#: may create a second post; a retried GET cannot.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Statuses that mean "I did not process this" rather than "something went wrong
#: while I was processing it". Safe to replay even for a POST, because the
#: platform is telling us it never got as far as doing anything.
_REJECTED_WITHOUT_PROCESSING = frozenset({429, 503})

#: Statuses worth a second go at all. 500/502/504 are ambiguous for a POST —
#: the write may well have landed — so :func:`_is_retryable` only replays them
#: for idempotent methods.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

#: How many hops a user-supplied host is allowed to bounce the request through
#: before Pulse stops following. Matches ``link_check``'s budget.
_MAX_REDIRECTS = 10


class PublishError(RuntimeError):
    """Publishing failed for a reason that might not recur.

    :attr:`status_code` carries the HTTP status the failure was translated from,
    when there was a response at all — ``None`` for a connect error, a read
    timeout, or an error an adapter raised on its own reading of a body.

    It exists because "the platform said 404" and "the platform said nothing
    useful" are the same type and must not always be the same decision. The
    caller that needs the distinction is
    :meth:`~app.services.publishers.git.GitAdapter._existing_sha`: a 404 there
    is the ordinary "this is a new post" case, and every other failure is "I was
    not allowed to look", which is not the same statement and must not be
    answered by committing as though the file were new.
    """

    #: The HTTP status behind this failure, or ``None`` when there was no
    #: response. Stamped by :meth:`Adapter._translate`; adapters that raise on
    #: their own reading of a body leave it unset, which is correct — those
    #: failures are not a status.
    status_code: int | None = None


class CredentialError(PublishError):
    """The stored credentials were rejected. Retrying will not help."""


class RateLimited(PublishError):
    """The platform refused the request and asked us to come back later.

    Distinct from a plain :class:`PublishError` for one reason: it carries the
    platform's own answer to "when?". Retrying a rate limit on the caller's
    schedule rather than the platform's is how a soft limit becomes a hard ban,
    so ``retry_after`` is honoured by parking the publication until then (see
    ``app.services.publishing_service.execute``).

    Not only 429: a 503 that carries a ``Retry-After`` is the same statement in
    a different status — "not now, come back at *this* time" — and is translated
    to this type so it gets the same treatment. A 503 without one is a plain
    :class:`PublishError`, because then there is nothing to honour.

    ``retry_after`` is ``None`` when the platform declined to say — the caller
    falls back to its own backoff. Only a 429 can produce that; the 503 arm only
    fires when the header parsed.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotImplementedAdapter(PublishError):
    """The platform is known but its adapter is not finished.

    A distinct type so the API can answer "why can't I publish here?" with
    something better than a generic failure, and so the registry can report
    capabilities without special-casing.
    """


class RefusedHost(CredentialError):
    """The adapter was pointed somewhere Pulse will not send a request.

    A subclass of :class:`CredentialError` because the two answers a caller needs
    are the same: stop retrying, and tell the user to fix the connection. The
    distinct type exists so the settings page can say *why* — "that address is
    inside the network" is a different fix from "that token expired".
    """


class UnsupportedOption(PublishError):
    """The adapter works, but this platform cannot do what was asked.

    Terminal, like :class:`CredentialError`: retrying does not give a platform a
    feature it does not have. The case that motivates it is ``as_draft`` on the
    social platforms — Mastodon and Bluesky have no draft state, and the only
    honest answers are "refuse" or "publish it live anyway". Refusing is the one
    that cannot surprise somebody who ticked a box to *avoid* going live.
    """


@dataclass(frozen=True)
class CredentialField:
    """One thing the user has to paste into the settings page."""

    key: str
    label: str
    help_text: str = ""
    #: Rendered as a password field and never echoed back by the API.
    secret: bool = True
    required: bool = True


@dataclass(frozen=True)
class PublishRequest:
    """A piece of content, ready to be adapted for one platform.

    Deliberately a flat value object rather than the ORM row: adapters run in a
    worker and must not lazy-load a relationship off a closed session.
    """

    title: str
    body_markdown: str
    excerpt: str
    meta_description: str
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    #: The primary SEO keyword this piece targets. Drives the SEO audit score.
    focus_keyword: str = ""
    #: URL-safe identifier for this piece — the filename a Git destination
    #: writes to, and the ``utm_content`` value on its share link.
    slug: str = ""
    #: The URL of the original, when this is a syndicated copy. Sent verbatim as
    #: ``rel=canonical`` and therefore **never** campaign-tagged: a canonical
    #: that differs from the original's real address is worse than none.
    canonical_url: str | None = None
    #: The campaign-tagged link a social post should point at. Built from the
    #: canonical (or the project URL) by ``app.services.utm``; see :attr:`link`.
    share_url: str | None = None
    #: Absolute URL of the feed/preview image. Every platform that supports one
    #: fetches it itself, so this stays a URL all the way down.
    cover_image_url: str | None = None
    #: The project's live URL — the link social posts point at.
    project_url: str | None = None
    project_name: str = ""
    #: Publish as a draft on the platform rather than going live. Used by the
    #: "stage everything, publish by hand" workflow.
    as_draft: bool = False
    #: A stable key for this (content, platform) pair, for the platforms that
    #: accept one. Retries are the reason: a request that succeeds and then times
    #: out on the way back is indistinguishable from one that failed, and without
    #: a key the retry posts a second copy.
    idempotency_key: str | None = None
    #: The language the four text fields above are actually in.
    #:
    #: Defaulted to English, which every request was implicitly until
    #: translations existed. Carried rather than inferred because an adapter
    #: cannot tell by looking — the destination that needs it most is the Git
    #: publisher, whose front matter has a ``lang`` field that drives the
    #: ``<html lang>`` of the generated page, and a page of French served as
    #: ``lang="en"`` is read aloud by a screen reader in an English accent.
    language: str = "en"

    @property
    def link(self) -> str | None:
        """The single URL a post should point a reader at.

        The tagged share link when there is one, then the canonical, then the
        project's own page. Adapters use this rather than picking among the
        three themselves, so attribution cannot be lost by one adapter reaching
        for ``canonical_url`` directly.
        """
        return self.share_url or self.canonical_url or self.project_url


@dataclass(frozen=True)
class PublishResult:
    """Where the content ended up."""

    external_id: str
    external_url: str
    #: Anything worth keeping that isn't the id or the URL.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricsSnapshot:
    """One platform's engagement numbers. Every field is optional.

    ``None`` means "this platform does not report it", which is different from
    zero and must stay different — averaging a missing view count as 0 makes
    every LinkedIn post look like a failure.
    """

    views: int | None = None
    reads: int | None = None
    clicks: int | None = None
    reactions: int | None = None
    comments: int | None = None
    #: Boosts, reposts, retweets. Distinct from a reaction: it is the one signal
    #: that extends a post's reach rather than describing it.
    shares: int | None = None


#: A preflight finding that stops the piece going to this platform at all.
PREFLIGHT_ERROR = "error"
#: A preflight finding the piece survives — it will publish, but not intact.
#: Truncation is the whole of this category today: a 900-word excerpt reaching
#: Bluesky is not a failure, it is 300 characters of it and a link.
PREFLIGHT_WARNING = "warning"


@dataclass(frozen=True)
class PreflightFinding:
    """One thing wrong — or worth knowing — about this piece on this platform.

    The distinction between the two levels is what a caller can do about it.
    :data:`PREFLIGHT_ERROR` means the platform will refuse the post or the
    adapter will raise before it is sent, so publishing is a wasted attempt and
    a ``failed`` row. :data:`PREFLIGHT_WARNING` means it will publish and be
    different from what is on screen — which is a thing to be told *before*
    pressing the button, because on the short-form destinations it is not
    recoverable afterwards.

    Carries the numbers as well as the sentence, because the sentence alone
    ("this will be shortened") cannot be acted on and "this is 412 characters
    over" can.
    """

    level: str
    message: str
    #: What the platform allows, when the finding is about a limit.
    limit: int | None = None
    #: What this piece actually measures against that limit.
    actual: int | None = None

    @property
    def is_error(self) -> bool:
        """Whether this finding stops the publish rather than merely shaping it."""
        return self.level == PREFLIGHT_ERROR

    def as_dict(self) -> dict[str, Any]:
        """The finding as JSON, omitting the numbers when it is not about a limit.

        ``limit`` and ``actual`` are dropped rather than sent as ``null`` so a
        UI can treat their presence as "this is a measurement" and render the
        pair, without a second flag saying which findings carry one.
        """
        body: dict[str, Any] = {"level": self.level, "message": self.message}
        if self.limit is not None:
            body["limit"] = self.limit
        if self.actual is not None:
            body["actual"] = self.actual
        return body


class Adapter(ABC):
    """Base class for platform adapters."""

    #: Which platform this serves.
    platform: Platform
    #: Human name for the settings page.
    display_name: str
    #: What the user must supply to connect.
    credential_fields: tuple[CredentialField, ...] = ()
    #: False for adapters that are scaffolded but not finished.
    implemented: bool = False
    #: Whether :meth:`fetch_metrics` returns anything real.
    supports_metrics: bool = False
    #: Set when there is something the user should know before connecting —
    #: surfaced verbatim in the UI. Used for platforms whose API access is
    #: restricted or deprecated.
    caveat: str = ""
    #: True when this adapter's base URL comes from a credential field rather
    #: than being a constant in the source. Those are the requests an outsider
    #: can aim, so :meth:`_request` guards them — see the module docstring.
    user_supplied_host: bool = False
    #: ``utm_medium`` for links published here. Declared per adapter because the
    #: distinction that matters downstream is what *kind* of channel this is —
    #: a full article syndicated to another blog behaves nothing like a 300
    #: character post with a link on it, and lumping both under "referral"
    #: throws away the only free segmentation available.
    utm_medium: str = "referral"
    #: Whether a URL from this platform can stand as the piece's canonical — the
    #: address every syndicated copy points at as the original.
    #:
    #: True for anywhere that hosts the *article*: the project's own blog, Dev.to,
    #: Medium, Hashnode, WordPress. False for the channels that carry a link to it
    #: instead — a Bluesky post is 300 characters and a URL, and a Buttondown
    #: archive page is an issue of a newsletter. Naming one of those as the
    #: canonical tells a crawler the microblog post *is* the article and the real
    #: one is the copy, which is the opposite of what syndication is for.
    #:
    #: Separate from :attr:`utm_medium` deliberately, though today they agree:
    #: one is an analytics label the user can reasonably want to change, the
    #: other decides what ``rel=canonical`` says.
    hosts_canonical: bool = True
    #: Whether this destination publishes to an address the *user* controls,
    #: rather than to somebody else's platform. Only used to break the tie when
    #: a project names no canonical platform and a batch contains more than one
    #: article destination: the copy on the user's own domain is the original,
    #: and the one on Dev.to is the syndicated copy, never the other way round.
    owns_domain: bool = False
    #: Whether the platform's API can change the headline of a post that is
    #: already live.
    #:
    #: This is the capability :mod:`app.services.headlines` depends on and the
    #: reason it cannot be assumed. A headline swap changes Pulse's copy of the
    #: title; unless the destination is told, readers keep seeing the old one
    #: while the engagement they generate is credited to the new one. Half the
    #: destinations here genuinely cannot be told — a Bluesky post has no title
    #: and a sent Buttondown issue is in inboxes — so the honest answer is to
    #: declare the capability and let the attribution exclude what it cannot
    #: reach, rather than to quietly measure the wrong thing.
    supports_title_update: bool = False

    @abstractmethod
    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Push *request* to the platform. Raises on failure."""

    def verify(self, credentials: dict[str, Any]) -> str:
        """Check the credentials and return a display name for the account.

        Called when a connection is saved so the user finds out immediately,
        rather than when a scheduled post fails at 3am.
        """
        raise NotImplementedAdapter(
            f"{self.display_name} cannot verify credentials yet"
        )

    def preflight(self, request: PublishRequest) -> list[PreflightFinding]:
        """What this platform will make of *request*, without contacting it.

        Answers the question the publish path could only answer by trying: does
        this piece fit here, and will it arrive intact. The default is "nothing
        to say", which is the right answer for the article destinations — a
        blog post going to somewhere that hosts blog posts has no format to
        fail.

        **No credentials, no network.** Every check here reads the request and
        the adapter's own constants, so this can run on a draft, on a piece
        whose platform is not connected yet, and on every destination at once
        for a preview panel. Anything that needs the platform's opinion
        (whether a tag exists, whether a slug collides) belongs in
        :meth:`publish`, which is where the answer can be trusted.

        **Deliberately not a gate.** Nothing refuses to publish on the strength
        of this. The adapters already handle their own limits — Bluesky
        composes to 300 characters whatever it is handed — and a preflight that
        blocked a publish would be a second implementation of those rules, free
        to disagree with the first. This reports; the caller decides.
        """
        return []

    def update_title(
        self, request: PublishRequest, credentials: dict[str, Any], external_id: str
    ) -> None:
        """Change the headline of the post already live at *external_id*.

        Only the title. The body, tags and canonical are left exactly as
        published — a headline test changes one variable, and re-sending a body
        that has since been edited in Pulse would smuggle an unreviewed
        revision onto a live post under cover of a title swap.

        Raises :class:`NotImplementedAdapter` by default. Adapters that
        override this must also set :attr:`supports_title_update`, and the two
        are checked against each other in the test suite: an adapter that
        claims the capability without implementing it would have its
        publications counted as evidence for a headline they never carried.
        """
        raise NotImplementedAdapter(
            f"{self.display_name} cannot change the title of a post that is already live"
        )

    def fetch_metrics(
        self, external_id: str, credentials: dict[str, Any]
    ) -> MetricsSnapshot:
        """Current engagement for a published post.

        The default returns an empty snapshot: a platform with no stats API is
        normal, not an error, and the metrics poller should skip it quietly.
        """
        return MetricsSnapshot()

    # -- helpers shared by the HTTP adapters -------------------------------- #

    def _require(self, credentials: dict[str, Any], *keys: str) -> tuple[str, ...]:
        """Pull required credential values, raising a clear error if missing."""
        values = []
        for key in keys:
            value = str(credentials.get(key) or "").strip()
            if not value:
                raise CredentialError(
                    f"{self.display_name} connection is missing '{key}'. "
                    "Reconnect it in Settings."
                )
            values.append(value)
        return tuple(values)

    def _json(self, response: httpx.Response) -> Any:
        """The response body decoded, as a :class:`PublishError` if it will not.

        ``response.json()`` raises ``json.JSONDecodeError`` — a ``ValueError``,
        and not a :class:`PublishError`. Every adapter called it bare, so a 200
        carrying something that is not JSON (a CDN's HTML error page, a captive
        portal, a body cut short mid-stream) went past
        ``publishing_service.execute``'s ``PublishError`` branch and into its
        ``except Exception`` catch-all, which fails the publication
        **terminally**. A transient blip at the edge of someone else's
        infrastructure therefore burned the post permanently, with its retry
        budget untouched, and the piece was left needing a hand-retry nobody
        knew to do.

        Raised as a plain :class:`PublishError` because that is exactly what it
        is: possibly transient, worth the retry budget.
        """
        try:
            return response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type") or "no content-type"
            error = PublishError(
                f"{self.display_name} returned a non-JSON body "
                f"({response.status_code}, {content_type})"
            )
            error.__cause__ = exc
            raise error from exc

    def _json_object(self, response: httpx.Response) -> dict[str, Any]:
        """The response body as an object, as a :class:`PublishError` if it is not.

        :meth:`_json` covers the body that will not decode. This covers the one
        that decodes into the wrong thing — a 200 carrying ``["maintenance"]``
        or a bare string — which every caller then reached into with ``.get``.
        ``AttributeError`` is not a :class:`PublishError`, so it fell to
        ``publishing_service.execute``'s ``except Exception`` and failed the
        publication **terminally**, with its retry budget untouched: the same
        harm ``_json`` exists to prevent, one layer further in. The reasoning is
        :meth:`HashnodeAdapter._gql`'s, which has carried this guard inline since
        a GraphQL body came back as a list; a platform does not have to speak
        GraphQL to have a gateway answer for it.

        ``None`` reads as an empty object rather than an error, which is what
        every call site meant by the ``or {}`` this replaces: the adapter's own
        "returned no post id" check is a better message than anything this
        function could invent, and it is the next line in each of them.
        """
        data = self._json(response)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise PublishError(
                f"{self.display_name} returned a JSON "
                f"{type(data).__name__} where an object was expected "
                f"({response.status_code})"
            )
        return data

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """One HTTP call with uniform error translation and safe retries.

        Auth failures become :class:`CredentialError` so the caller stops
        retrying and marks the connection invalid; a 429 becomes
        :class:`RateLimited` carrying the platform's own ``Retry-After``;
        everything else stays a retryable :class:`PublishError`.

        Transient failures are retried in-process up to
        ``PUBLISH_REQUEST_RETRIES`` times with exponential backoff and jitter,
        but only when replaying the call cannot change the outcome — see
        :func:`_is_retryable`. Pass ``retries=0`` to opt one call out.
        """
        budget = settings.publish_request_retries if retries is None else retries
        attempt = 0

        while True:
            error: PublishError
            response: httpx.Response | None = None
            try:
                response = self._send(
                    method, url, headers=headers, json_body=json_body, params=params
                )
            except httpx.HTTPError as exc:
                error = PublishError(f"{self.display_name}: {friendly_network_error(exc)}")
                error.__cause__ = exc
            else:
                failure = self._translate(response)
                if failure is None:
                    return response
                error = failure

            attempt += 1
            if attempt > budget or not _is_retryable(method, error, response):
                raise error

            delay = _backoff_delay(attempt, error)
            logger.info(
                "%s %s failed (%s) — retry %d/%d in %.1fs",
                self.display_name,
                method,
                error,
                attempt,
                budget,
                delay,
            )
            _sleep(delay)

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        json_body: Any,
        params: dict | None,
    ) -> httpx.Response:
        """One HTTP exchange, with redirects handled according to the host.

        For an adapter whose base URL is a constant, httpx follows redirects
        itself — the destination is Dev.to, and Dev.to is allowed to move its
        own endpoints around.

        For an adapter pointed at a host the user typed, every hop is resolved
        and checked first. Letting httpx follow would hand the choice of final
        address to a ``Location`` header: a site that passes the pre-flight can
        answer ``302 → http://169.254.169.254/``, and the guard would have
        checked a host that never received the request.
        """
        def call(target: str, follow: bool) -> httpx.Response:
            return httpx.request(
                method,
                target,
                headers=headers,
                json=json_body,
                params=params,
                timeout=settings.publish_timeout_seconds,
                follow_redirects=follow,
            )

        if not self.user_supplied_host:
            return call(url, True)

        current = url
        for _ in range(_MAX_REDIRECTS):
            self._require_public_url(current)
            response = call(current, False)
            if not response.is_redirect:
                return response
            # httpx builds the next request even when it is not following, and
            # it is the better source: it applies the method and body changes a
            # 303 requires. Joining the header is the fallback for a transport
            # that does not.
            following = response.next_request
            location = response.headers.get("location", "")
            if following is None and not location:
                # A 3xx with nowhere to go. Hand it back and let _translate
                # judge it rather than inventing a destination.
                return response
            current = (
                str(following.url)
                if following is not None
                else str(httpx.URL(current).join(location))
            )

        raise PublishError(
            f"{self.display_name} redirected more than {_MAX_REDIRECTS} times — "
            "point the connection at the final address."
        )

    def _require_public_url(self, url: str) -> None:
        """Refuse a URL that is not an outward-facing http(s) address.

        ``link_check.unreachable_reason`` does the resolving: one private,
        loopback or link-local answer among a host's addresses is enough to
        refuse it, because nothing says httpx would pick the same record this
        check looked at.
        """
        if not url.lower().startswith(("http://", "https://")):
            raise RefusedHost(
                f"{self.display_name} needs an http:// or https:// address — "
                f"got {url[:80]!r}."
            )
        reason = link_check.unreachable_reason(url)
        if reason:
            raise RefusedHost(
                f"Pulse will not send {self.display_name} requests to that "
                f"address — {reason}"
            )

    def _translate(self, resp: httpx.Response) -> PublishError | None:
        """The error a response deserves, or ``None`` when it is a success.

        Whatever comes back is stamped with the status it was translated from,
        so a caller that needs to tell one 4xx from another does not have to
        parse the message. See :attr:`PublishError.status_code`.
        """
        error = self._classify(resp)
        if error is not None:
            error.status_code = resp.status_code
        return error

    def _classify(self, resp: httpx.Response) -> PublishError | None:
        """The error a response deserves, before the status is stamped on it."""
        if resp.status_code in (401, 403):
            return CredentialError(
                f"{self.display_name} rejected the credentials "
                f"({resp.status_code}): {_short(resp.text)}"
            )
        if resp.status_code == 429:
            return RateLimited(
                f"{self.display_name} rate-limited the request",
                retry_after=_retry_after(resp),
            )
        if resp.status_code == 503:
            # RFC 9110 §10.2.3 puts ``Retry-After`` on 503 for the same reason it
            # puts it on 429, and it answers the same question: the platform has
            # told us when it will be ready. Reading it only for 429 meant a
            # maintenance window announcing "back in an hour" was answered with
            # Pulse's own one-second backoff — the whole in-process budget spent
            # inside the first blink of the outage, and then a row parked on
            # ``retry_defer_seconds`` as though nobody had said anything, when
            # the platform had already given the one number worth having.
            #
            # Only a *usable* header takes this branch. A bare 503 stays a plain
            # ``PublishError`` and keeps the behaviour it has always had: still
            # retryable, still replayable even for a POST — ``503`` remains in
            # :data:`_REJECTED_WITHOUT_PROCESSING` — just on our schedule,
            # because there is nothing else to go on.
            wait = _retry_after(resp)
            if wait is not None:
                return RateLimited(
                    f"{self.display_name} is unavailable (503) and asked for "
                    f"{round(wait)}s: {_short(resp.text)}",
                    retry_after=wait,
                )
        if resp.status_code >= 400:
            return PublishError(
                f"{self.display_name} returned {resp.status_code}: {_short(resp.text)}"
            )
        return None


def _is_retryable(
    method: str, error: PublishError, response: httpx.Response | None
) -> bool:
    """Whether replaying this call is both useful and safe.

    Useful rules out the terminal failures: a rejected credential is rejected
    just as hard the second time. Safe is the interesting half — a POST that
    failed *after* the platform started processing it may already have created
    the post, so replaying it risks a duplicate. Three cases pass:

    * the method is idempotent, so a replay cannot add anything;
    * the platform answered 429 or 503, which say "I did not process this";
    * the request never reached the platform at all (a connect error).

    A read timeout on a POST fails all three, and deliberately: the request was
    sent, and nobody knows whether it landed.

    One case is useful but *not this layer's job*: a rate limit whose
    ``Retry-After`` is longer than the in-process backoff ceiling. Sleeping the
    ceiling and going again means coming back before the platform said to, which
    is precisely how a soft limit becomes a ban — and it burns the retry budget
    doing it, so the publication reaches
    :func:`app.services.publishing_service._defer` with nothing left. Handing it
    upwards instead parks the row until the platform's own time, which can be an
    hour rather than the thirty seconds this loop can hold a worker for.
    """
    if isinstance(error, CredentialError | NotImplementedAdapter | UnsupportedOption):
        return False

    if (
        isinstance(error, RateLimited)
        and error.retry_after is not None
        and error.retry_after > settings.publish_retry_max_backoff_seconds
    ):
        return False

    if method.upper() in _IDEMPOTENT_METHODS:
        return response is None or response.status_code in _TRANSIENT_STATUSES

    if response is not None:
        return response.status_code in _REJECTED_WITHOUT_PROCESSING

    # No response: retry only when we know the request never got out.
    return isinstance(error.__cause__, httpx.ConnectError | httpx.ConnectTimeout)


def _backoff_delay(attempt: int, error: PublishError) -> float:
    """How long to wait before retry number *attempt* (1-based).

    A ``Retry-After`` from the platform wins outright — guessing shorter than
    what it asked for is how a soft limit becomes a ban. It is never clamped
    down to the ceiling here: :func:`_is_retryable` has already declined
    anything longer than the ceiling, so a wait that reaches this function is
    one the loop can honour in full. Otherwise exponential backoff with full
    jitter, so several publications failing at once do not come back in
    lockstep.
    """
    ceiling = settings.publish_retry_max_backoff_seconds
    if isinstance(error, RateLimited) and error.retry_after is not None:
        return min(error.retry_after, ceiling)

    window = min(settings.publish_retry_backoff_seconds * (2 ** (attempt - 1)), ceiling)
    return random.uniform(window / 2, window)


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait, from the ``Retry-After`` header. ``None`` if unusable.

    The header comes in two shapes (RFC 9110 §10.2.3): a delay in seconds, or
    an HTTP-date. Both are handled; anything else, or a date already in the
    past, reads as "no guidance" rather than an error.
    """
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _sleep(seconds: float) -> None:
    """Indirection so tests can retry without actually waiting."""
    time.sleep(seconds)


def _short(text: str, limit: int = 300) -> str:
    """A response body trimmed to something that fits in an error column."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


__all__ = [
    "Adapter",
    "CredentialError",
    "CredentialField",
    "MetricsSnapshot",
    "NotImplementedAdapter",
    "PublishError",
    "PublishRequest",
    "PublishResult",
    "RateLimited",
    "RefusedHost",
    "UnsupportedOption",
]
