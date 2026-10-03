"""Bounding what a third party's failure message is allowed to cost us.

Every ``last_error`` / ``error`` column in this schema is ``Text``: no length
limit in the database, and none in the string being written either. The strings
come from outside — an adapter quoting a platform's response body, a webhook
endpoint's reply, an RSS parser's complaint about a feed. Any of them can be a
megabyte of someone else's HTML, and all of them are written on *every* failed
attempt and rendered straight into a list view.

Bounding lives here, in a module that imports nothing of Pulse's own, because
the four places that record a failure are spread across a router, two services
and the publishing pipeline — and ``publishing_service`` already imports
``webhooks``, so the helper cannot live there without the reverse import
becoming a cycle.

:func:`redact` is here for the same reason and answers the other half of the
question: bounding decides how much of somebody else's message is kept, and
redaction decides that the part of it which is *ours* is not.
"""
from __future__ import annotations

from collections.abc import Iterable

import httpx

#: How much of a failure message is worth keeping on the row.
#:
#: Adapter messages quote what the platform said, and several of them quote the
#: whole body when it is not the shape they expected — ``f"Hashnode returned no
#: post: {data}"``. That body is not ours and has no size limit; ``error`` is a
#: ``Text`` column with none either, and it is written again on every attempt
#: and rendered in the publications list. A megabyte of someone else's JSON in
#: a field the UI shows is a bad row and a slow page, and the part that says
#: what went wrong is in the first line regardless.
MAX_ERROR_CHARS = 2000


#: What a redacted credential is replaced with. Deliberately visible: somebody
#: reading a failure should be able to tell that a value was removed, or the
#: message reads as though the platform said nothing there.
REDACTED = "[redacted]"

#: Below this length a "credential" is not one, and removing every occurrence of
#: it would do more damage to the message than leaving it. A four-character
#: value appears inside ordinary words; a real token does not.
_MIN_REDACTABLE = 8


def clip_error(error: str) -> str:
    """A failure message bounded to :data:`MAX_ERROR_CHARS`.

    Applied where the message is written to a row rather than where it is
    raised, so a new adapter — or a new platform behind an existing one —
    cannot forget it.
    """
    if len(error) <= MAX_ERROR_CHARS:
        return error
    return error[: MAX_ERROR_CHARS - 1].rstrip() + "…"


def redact(error: str, secrets: Iterable[object]) -> str:
    """*error* with any of *secrets* appearing in it replaced by :data:`REDACTED`.

    Adapter failures quote what the platform said — ``_translate`` puts the
    response body in the message on purpose, because "WordPress returned 400"
    with nothing after it is not a message anyone can act on. That body is
    written by somebody else, and some APIs validate by echoing: *"invalid
    api_key: ghp_…"*.

    Pulse then stores the whole thing in ``Publication.error`` /
    ``PlatformConnection.last_error`` — ``Text`` columns, in plaintext, right
    next to ``encrypted_credentials`` — writes it again on every attempt, and
    renders it in the publications list. The token is encrypted at rest and
    would arrive in the database beside it in the clear, put there by the
    failure path rather than by any write that thinks it is storing a
    credential.

    Applied where the failure leaves the adapter, which is the last point the
    credential values are known, and for the same reason :func:`clip_error` is
    applied where the row is written: a new adapter cannot forget it.

    Values shorter than :data:`_MIN_REDACTABLE` are left alone — see the note
    there. Ordered longest-first so a credential that contains another (a
    Bluesky app password and the handle it was issued for) does not leave half
    of itself behind.
    """
    for secret in sorted(
        {str(s) for s in secrets if s}, key=len, reverse=True
    ):
        if len(secret) >= _MIN_REDACTABLE:
            error = error.replace(secret, REDACTED)
    return error


_HTTPX_FRIENDLY: dict[type, str] = {
    httpx.ConnectTimeout: "Connection timed out",
    httpx.ReadTimeout: "The server took too long to respond",
    httpx.WriteTimeout: "Sending the request timed out",
    httpx.PoolTimeout: "No connection available (pool exhausted)",
    httpx.ConnectError: "Could not connect to the server",
    httpx.ReadError: "The connection was lost while reading the response",
    httpx.WriteError: "The connection was lost while sending the request",
    httpx.CloseError: "Error closing the connection",
}


def friendly_network_error(exc: httpx.HTTPError) -> str:
    """A user-facing description of an httpx transport error.

    The raw exception string can include internal class names, URLs with
    credentials, or low-level socket details.  This returns a short,
    actionable sentence the UI can show without leaking internals.
    """
    for cls, msg in _HTTPX_FRIENDLY.items():
        if isinstance(exc, cls):
            return msg
    if isinstance(exc, httpx.TimeoutException):
        return "The request timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"The server returned HTTP {exc.response.status_code}"
    return "A network error occurred while connecting"


def sanitize_unexpected_error(exc: Exception) -> str:
    """A safe one-liner for an exception whose message may leak internals.

    Used for the ``except Exception`` catch-all arms that record a failure
    on a row.  The raw ``str(exc)`` can contain file paths, class names, or
    connection strings; this returns only the exception's type name so the
    operator can correlate with the log line (which has the full traceback)
    without exposing internals to the UI.
    """
    return f"Internal error ({type(exc).__name__})"
