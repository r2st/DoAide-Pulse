"""The error half of the API contract.

Every failure Pulse returns is ``{"detail": "..."}`` — ``HTTPException`` renders
that shape, the unhandled-exception handler in :mod:`app.main` matches it
deliberately, and so do the three middleware refusals that never reach a route.
The one exception is the 422 FastAPI generates itself, whose ``detail`` is a
list of per-field objects; FastAPI documents that one on its own.

None of it appeared in the schema. Ninety-one endpoints declared a success model
and nothing else, so the generated OpenAPI said what happened when things worked
and was silent about every other outcome — which is the half a client actually
has to write code for. A generated client got ``Any`` for the error branch, and
a reader of ``/docs`` could not tell a 404 that means "no such project" from one
that means "you do not own it" without reading the source.

The maps below are the vocabulary. :func:`errors` composes them, so an endpoint
declares the codes it can actually produce and gets the descriptions for free —
consistent wording across the whole surface, and one place to change it.
"""
from __future__ import annotations

from typing import Any

from fastapi import status
from pydantic import BaseModel, Field


class ErrorOut(BaseModel):
    """The body of every non-422 error response."""

    detail: str = Field(
        description="A human-readable explanation, safe to show the user.",
        examples=["That project does not exist."],
    )


def _response(description: str) -> dict[str, Any]:
    return {"model": ErrorOut, "description": description}


#: One entry per status code the API actually produces, with the description
#: that code means *here*. Keyed by int because that is what FastAPI's
#: ``responses=`` takes.
_CATALOGUE: dict[int, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: _response(
        "The request is well-formed but cannot be carried out — a value the "
        "schema alone cannot check, or a state the resource is not in."
    ),
    status.HTTP_401_UNAUTHORIZED: _response(
        "No bearer token, or a token that is expired, malformed, or belongs to "
        "a deactivated account."
    ),
    status.HTTP_403_FORBIDDEN: _response(
        "Authenticated, but not allowed to do this."
    ),
    status.HTTP_404_NOT_FOUND: _response(
        "No such resource — or one owned by somebody else. The two are "
        "deliberately indistinguishable: a 403 on another user's row confirms "
        "the row exists."
    ),
    status.HTTP_409_CONFLICT: _response(
        "The resource is in a state that makes this request meaningless, and "
        "retrying will not change that."
    ),
    status.HTTP_412_PRECONDITION_FAILED: _response(
        "The ``If-Match`` you sent names a version the resource has moved past "
        "— somebody else wrote it after you loaded it. Reload, reapply, retry."
    ),
    status.HTTP_413_CONTENT_TOO_LARGE: _response(
        "The request body is over the limit. Refused by middleware before any "
        "route sees it."
    ),
    status.HTTP_422_UNPROCESSABLE_CONTENT: _response(
        "The body did not validate. FastAPI's own 422 carries a list of "
        "per-field problems; the ones raised by a route carry a single string."
    ),
    status.HTTP_429_TOO_MANY_REQUESTS: _response(
        "Rate limited. Retry after the window in the response."
    ),
    status.HTTP_500_INTERNAL_SERVER_ERROR: _response(
        "Something failed inside Pulse. The body never carries detail; quote "
        "the X-Request-ID header, which every response has."
    ),
    status.HTTP_501_NOT_IMPLEMENTED: _response(
        "The platform is recognised but its adapter is not finished."
    ),
    status.HTTP_502_BAD_GATEWAY: _response(
        "A service Pulse depends on answered badly — a platform API, a feed, "
        "or a repository host. Not necessarily a problem with the request."
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: _response(
        "A dependency Pulse cannot run without is unreachable."
    ),
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """The ``responses=`` map for *codes*, in ascending order.

    Raises ``KeyError`` for a code with no entry above, which is the point: a
    new failure mode should be described once here rather than spelled out at
    each endpoint that can return it.
    """
    return {code: _CATALOGUE[code] for code in sorted(set(codes))}


#: What almost every authenticated endpoint can return regardless of what it
#: does: no token, and a body too big for the middleware to pass on.
AUTHENTICATED = (
    status.HTTP_401_UNAUTHORIZED,
    status.HTTP_413_CONTENT_TOO_LARGE,
)

#: An authenticated endpoint addressing one row by id.
OWNED = (*AUTHENTICATED, status.HTTP_404_NOT_FOUND)

__all__ = ["AUTHENTICATED", "OWNED", "ErrorOut", "errors"]
