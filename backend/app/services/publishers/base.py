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
POSTs, because a duplicate article is worse than a failed one.
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


class PublishError(RuntimeError):
    """Publishing failed for a reason that might not recur."""


class CredentialError(PublishError):
    """The stored credentials were rejected. Retrying will not help."""


class RateLimited(PublishError):
    """The platform refused the request and asked us to come back later.

    Distinct from a plain :class:`PublishError` for one reason: it carries the
    platform's own answer to "when?". Retrying a rate limit on the caller's
    schedule rather than the platform's is how a soft limit becomes a hard ban,
    so ``retry_after`` is honoured by parking the publication until then (see
    ``app.services.publishing_service.execute``).

    ``retry_after`` is ``None`` when the platform declined to say — the caller
    falls back to its own backoff.
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
    #: ``utm_medium`` for links published here. Declared per adapter because the
    #: distinction that matters downstream is what *kind* of channel this is —
    #: a full article syndicated to another blog behaves nothing like a 300
    #: character post with a link on it, and lumping both under "referral"
    #: throws away the only free segmentation available.
    utm_medium: str = "referral"

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
                response = httpx.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=settings.publish_timeout_seconds,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                error = PublishError(f"{self.display_name} request failed: {exc}")
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

    def _translate(self, resp: httpx.Response) -> PublishError | None:
        """The error a response deserves, or ``None`` when it is a success."""
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
    """
    if isinstance(error, CredentialError | NotImplementedAdapter | UnsupportedOption):
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
    what it asked for is how a soft limit becomes a ban. Otherwise exponential
    backoff with full jitter, so several publications failing at once do not
    come back in lockstep.
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
    "UnsupportedOption",
]
