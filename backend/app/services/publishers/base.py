"""The contract every publishing adapter implements.

An adapter is stateless: it takes a :class:`PublishRequest` and a credential
dict and returns a :class:`PublishResult`. It never touches the database, which
is what lets the publish task own retries, status transitions and error
recording in one place regardless of platform.

Failures are split into two kinds because the caller's response differs:

* :class:`CredentialError` — the token is wrong, expired or lacks a scope.
  Retrying is pointless; the connection is marked invalid and the user is asked
  to reconnect.
* :class:`PublishError` — anything else. Might be transient, so it is retried
  with backoff up to ``PUBLISH_MAX_RETRIES``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.models.publication import Platform

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Publishing failed for a reason that might not recur."""


class CredentialError(PublishError):
    """The stored credentials were rejected. Retrying will not help."""


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
    ) -> httpx.Response:
        """One HTTP call with uniform error translation.

        Auth failures become :class:`CredentialError` so the caller stops
        retrying and marks the connection invalid; everything else stays
        retryable.
        """
        try:
            resp = httpx.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=settings.publish_timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"{self.display_name} request failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise CredentialError(
                f"{self.display_name} rejected the credentials "
                f"({resp.status_code}): {_short(resp.text)}"
            )
        if resp.status_code == 429:
            raise PublishError(f"{self.display_name} rate-limited the request")
        if resp.status_code >= 400:
            raise PublishError(
                f"{self.display_name} returned {resp.status_code}: {_short(resp.text)}"
            )
        return resp


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
    "UnsupportedOption",
]
