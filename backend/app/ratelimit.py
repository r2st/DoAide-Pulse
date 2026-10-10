"""Request rate limiting (slowapi), on two different grounds.

**Audience.** If a caller can reach it with no bearer token, it carries a
limit. That is the whole unauthenticated surface, and it is worth listing
because it is easy to assume it is just login:

* ``/auth/login``, ``/auth/register``, ``/auth/password-reset*`` — password
  guessing, account creation, and reset-mail flooding, the classic three.
* ``/projects/{id}/feed.xml`` — the public RSS feed. Two database queries and
  an XML render for anyone who can guess a project id.
* ``/health`` — a Postgres round-trip and a fresh Redis connection per call.
* ``/webhooks/events``, ``/triggers/kinds`` — constant lists, limited for
  consistency rather than for cost.
* ``/triggers/inbound/{token}`` — limited separately, since a trigger fires a
  content generation and the token is the only thing gating it.

An earlier version of this docstring said everything outside ``/auth/*`` needed
a token. That stopped being true when the feed and the health probe landed, and
neither was limited — which is the failure mode a list like the one above is
meant to make visible, since the limit is only ever added by someone who
remembers the endpoint is public.

**Cost.** Audience was once the *only* ground, on the reasoning that a caller
who has authenticated is a caller Pulse has already decided to trust. That
holds for reads. It does not hold for the handful of endpoints where one
request spends something Pulse cannot make more of:

* the writing endpoints — ``/content/generate``, ``/content/ideas/{id}/write``,
  ``/content/{id}/repurpose``, ``/content/{id}/edit``,
  ``/content/{id}/headlines`` and ``/projects/{id}/ideas?refresh=true`` — each
  turn into at least one LLM call. The free-tier quotas they run on are metered
  per day and shared by every account on the install, so one client in a retry
  loop does not degrade its own service, it ends everyone's until midnight.
  :mod:`app.services.llm_router` handles an upstream *saying* no; nothing
  handled Pulse asking that many times in the first place.
* ``/projects/{id}/scan`` spends the install's GitHub API quota, which is one
  budget for all users and answers 403 to everybody once it is gone.
* ``/content/{id}/links`` fans one request out to ``link_check_max_urls``
  outbound requests, from Pulse's address, to hosts named in the document.

These are bucketed per *account* rather than per address — see
:func:`account_key`. Bucketing an authenticated endpoint by IP gets it wrong in
both directions: an office behind one NAT shares a budget it did not agree to
share, and anyone with a second address gets a second helping. The budgets are
set far above what a human clicking buttons produces and far below what a loop
produces, so the first thing they ever catch should be a bug.

Two deployment details shape the configuration:

* **The counter is per-process by default.** Production runs uvicorn with two
  workers, so an in-memory limit of 10/minute is really 20/minute. Set
  ``RATE_LIMIT_STORAGE_URI`` to a Redis URL to make the budget global.
* **The client address comes from Caddy.** Every request arrives from the
  reverse proxy, so ``request.client.host`` is one address for the whole
  internet and one bucket for every caller. See :func:`client_key`.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.security import decode_access_token


def client_key(request: Request) -> str:
    """The bucket a request counts against.

    When ``RATE_LIMIT_TRUST_FORWARDED_FOR`` is on, the **rightmost**
    X-Forwarded-For entry wins. Caddy appends the peer address it observed to
    whatever the client sent, so the last element is the one value in that
    header a caller cannot choose — taking the leftmost (the usual "original
    client" convention) would let anyone reset their own limit with a header.
    """
    if settings.rate_limit_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",")[-1].strip()
            if candidate:
                return candidate
    return get_remote_address(request)


def account_key(request: Request) -> str:
    """The bucket a *cost-limited* request counts against: the account.

    The subject is read straight out of the bearer token rather than off
    ``request.state``, because nothing guarantees that
    :func:`app.deps.get_current_user` has run by the time slowapi asks. The
    token is verified — signature and expiry both, via
    :func:`app.security.decode_access_token` — so the bucket cannot be chosen
    by anyone who has not already authenticated as its owner. A forged or
    expired token buys nothing: it falls through to :func:`client_key` and is
    refused by the endpoint's own dependency moments later anyway.

    The ``user:`` prefix keeps account buckets from ever colliding with the
    address buckets in the same storage — a user id is a small integer, and
    ``1`` is not a plausible address, but the two namespaces sharing a keyspace
    is the kind of thing that stops being true quietly.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        subject = decode_access_token(token.strip())
        if subject:
            return f"user:{subject}"
    return client_key(request)


def api_key_key(request: Request) -> str:
    """The bucket a ``/machine`` request counts against: the credential itself.

    The ``/machine`` surface is reached with an ``X-API-Key`` header rather than
    a bearer token, so neither of the two buckets above fits it. Address is
    wrong for the reason :func:`account_key` gives — the CI runners of one
    account share a NAT and would share a budget — and the account is wrong in
    the other direction, since the whole point of a per-project key is that the
    project's automation is metered separately from the person's browser.

    The bucket is the key's **prefix**, taken from the header without a database
    lookup: slowapi asks for a key before any dependency runs, and a limiter
    that opens a session is a limiter that falls over exactly when the thing it
    is limiting is happening.

    Unverified, and safe for the same reason ``account_key``'s token read is:
    the prefix is *inside* the credential, so a caller who wants to be
    authenticated has no choice about which bucket they land in. A caller who
    sprays invented prefixes does get a fresh bucket each time — and buys
    nothing but 401s, since the secret half still has to match a stored digest.
    That path is one indexed SELECT and a SHA-256, which is the cheapest thing
    on this router.

    Anything without a parseable token falls through to the address bucket, so
    a machine endpoint is never wholly unlimited.
    """
    header = request.headers.get("x-api-key", "").strip()
    if header:
        # Imported here rather than at module scope: app.services.api_keys
        # imports the models, and the models import nothing from this module —
        # a top-level import would make ratelimit part of that cycle for the
        # sake of one string split.
        from app.services.api_keys import split

        prefix = split(header)
        if prefix:
            return f"apikey:{prefix}"
    return client_key(request)


limiter = Limiter(
    key_func=client_key,
    storage_uri=settings.rate_limit_storage_uri or "memory://",
    enabled=settings.rate_limit_enabled,
    headers_enabled=True,
    # A dead Redis must not take the login page down with it: on a storage
    # error slowapi logs and allows the request through.
    swallow_errors=True,
    key_prefix="pulse",
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """429 in FastAPI's shape.

    slowapi's own handler answers ``{"error": ...}``; every other error in this
    API is ``{"detail": ...}`` and that is what the frontend reads, so a
    rate-limited login would otherwise surface as a blank toast.
    """
    response = JSONResponse(
        {"detail": "Too many requests — slow down and try again shortly."},
        status_code=429,
    )
    # Adds Retry-After / X-RateLimit-* so a client can back off sensibly.
    return request.app.state.limiter._inject_headers(
        response, request.state.view_rate_limit
    )


def reset() -> None:
    """Drop every counter. For tests, which would otherwise leak across cases."""
    limiter.reset()


__all__ = [
    "account_key",
    "api_key_key",
    "client_key",
    "limiter",
    "rate_limit_exceeded_handler",
    "reset",
]
