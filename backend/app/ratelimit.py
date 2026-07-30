"""Request rate limiting (slowapi) for the authentication surface.

Only ``/auth/*`` is limited. Those are the endpoints an outsider can reach
without a token, and the ones where an unlimited request rate is worth
something to an attacker: password guessing on ``/auth/login``, account
creation on ``/auth/register``, and reset-mail flooding on the password-reset
routes. Everything else already needs a valid bearer token.

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


limiter = Limiter(
    key_func=client_key,
    storage_uri=settings.rate_limit_storage_uri or "memory://",
    enabled=settings.rate_limit_enabled,
    headers_enabled=True,
    # A dead Redis must not take the login page down with it: on a storage
    # error slowapi logs and allows the request through.
    swallow_errors=True,
    key_prefix="herald",
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """429 in FastAPI's shape.

    slowapi's own handler answers ``{"error": ...}``; every other error in this
    API is ``{"detail": ...}`` and that is what the frontend reads, so a
    rate-limited login would otherwise surface as a blank toast.
    """
    response = JSONResponse(
        {"detail": f"Too many requests. Limit: {exc.detail}."}, status_code=429
    )
    # Adds Retry-After / X-RateLimit-* so a client can back off sensibly.
    return request.app.state.limiter._inject_headers(
        response, request.state.view_rate_limit
    )


def reset() -> None:
    """Drop every counter. For tests, which would otherwise leak across cases."""
    limiter.reset()


__all__ = ["client_key", "limiter", "rate_limit_exceeded_handler", "reset"]
