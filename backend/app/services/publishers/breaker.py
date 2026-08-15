"""A breaker in front of the platforms, and what it is and is not for.

:mod:`app.services.publishers.base` already answers the fast failures. A dropped
connection or a 503 from a load balancer rolling is retried in-process a handful
of times; anything that outlives that is parked on the publication row and
re-armed by the beat sweep on an escalating backoff, up to
``publish_max_retries``. Between them, one publication that meets a bad platform
is handled about as well as one publication can be.

What neither of them can see is the *second* publication. Dev.to being down is
one fact, and every row queued for Dev.to rediscovers it independently: each
spends its in-process retries, each burns an attempt off its budget, each waits
its own backoff and comes back to the same dead host. A queue of thirty rows
against a platform having a twenty-minute outage is thirty rows arriving at
terminal ``failed`` with nothing wrong with any of them — which is precisely the
shape of the failure that put ten pieces in ``failed`` on production, reached by
a different road.

So this holds the one fact the rows cannot share: *this account's route to this
platform is not working right now*. While it is open, a row is parked without
being tried and **without spending an attempt** — the retry budget is for
attempts that reached a platform, and charging a row for a request that was
never sent is how a piece runs out of retries during an outage it never touched.

**Keyed per account, not per platform.** The tempting key is the platform alone:
an outage is the platform's and one breaker would protect everybody. But the
failures that trip a breaker are not all outages. A rate limit is charged
against an API key, so it is one account's fact, and a global breaker would let
one busy account park every other account's posts — a cross-tenant denial of
service that Herald would be doing to itself. Keying per ``(platform, user)``
costs a real outage one threshold per account before it is noticed, which is a
few wasted requests, and that is the cheaper of the two mistakes by a distance.

**What counts as evidence.** Only failures that say something about the *route*:
a transient :class:`~app.services.publishers.base.PublishError`, a
:class:`~app.services.publishers.base.RateLimited`, a timeout. Explicitly not
:class:`~app.services.publishers.base.CredentialError`,
:class:`~app.services.publishers.base.NotImplementedAdapter`,
:class:`~app.services.publishers.base.UnsupportedOption` or ``NotConnected``:
each of those is a fact about the *request* — this token, this feature, this
account — and a working platform would refuse them all exactly the same way on
every attempt. A breaker that counted them would open on a platform that is
perfectly healthy, and then park the rows that would have succeeded.

A rate limit that named its own window opens the breaker for exactly that long
rather than for the standard cooldown, for the reason ``RateLimited`` exists at
all: coming back sooner than a platform asked is how a soft limit becomes a ban.
"""
from __future__ import annotations

import logging

from app.config import settings
from app.models.publication import Platform
from app.services.breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def key(platform: Platform, user_id: int) -> str:
    """The breaker key for one account's route to one platform.

    ``devto:7``. Flat rather than a tuple because
    :class:`~app.services.breaker.CircuitBreaker` keys on a string and the
    snapshot it produces is rendered straight into a diagnostics payload, where
    ``devto:7`` reads and greps better than ``('devto', 7)``.
    """
    return f"{platform.value}:{user_id}"


breaker = CircuitBreaker(
    threshold=settings.publish_breaker_threshold,
    cooldown_seconds=float(settings.publish_breaker_cooldown_seconds),
)


def is_open(platform: Platform, user_id: int) -> bool:
    """Whether this account's route to *platform* is being skipped right now."""
    return settings.publish_breaker_enabled and breaker.is_open(key(platform, user_id))


def seconds_remaining(platform: Platform, user_id: int) -> float:
    """How long that route stays shut. ``0.0`` when it is open for business."""
    return breaker.seconds_remaining(key(platform, user_id))


def record_success(platform: Platform, user_id: int) -> None:
    """A publish landed: forget whatever run of failures preceded it."""
    breaker.record_success(key(platform, user_id))


def record_failure(platform: Platform, user_id: int, *, retry_after: float | None = None) -> None:
    """Count one failure that says something about the route.

    *retry_after* is the platform's own answer to "when?", from a
    :class:`~app.services.publishers.base.RateLimited`. When it is given, the
    breaker opens for exactly that long instead of waiting for the failure
    threshold — the platform has already said it will not serve this account,
    and two more requests to confirm it is what the limit is there to stop.
    """
    if not settings.publish_breaker_enabled:
        return
    name = key(platform, user_id)
    if retry_after is not None and retry_after > 0:
        window = min(
            float(retry_after), float(settings.publish_breaker_max_cooldown_seconds)
        )
        breaker.open_for(name, window)
        logger.warning(
            "publishing breaker open for %s for %.0fs — the platform named the window",
            name,
            window,
        )
        return
    if breaker.record_failure(name):
        logger.warning(
            "publishing breaker open for %s for %ds after %d consecutive failures",
            name,
            settings.publish_breaker_cooldown_seconds,
            settings.publish_breaker_threshold,
        )


def snapshot() -> dict[str, dict[str, float]]:
    """Every route currently being counted or skipped, for diagnostics."""
    return breaker.snapshot()


def reset() -> None:
    """Forget everything — for tests and after a config change."""
    breaker.reset()


__all__ = [
    "breaker",
    "is_open",
    "key",
    "record_failure",
    "record_success",
    "reset",
    "seconds_remaining",
    "snapshot",
]
