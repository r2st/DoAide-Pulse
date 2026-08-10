"""Bounding what a third party's failure message is allowed to cost us.

Every ``last_error`` / ``error`` column in this schema is ``Text``: no length
limit in the database, and none in the string being written either. The strings
come from outside — an adapter quoting a platform's response body, a webhook
endpoint's reply, an RSS parser's complaint about a feed. Any of them can be a
megabyte of someone else's HTML, and all of them are written on *every* failed
attempt and rendered straight into a list view.

Bounding lives here, in a module with no imports of its own, because the four
places that record a failure are spread across a router, two services and the
publishing pipeline — and ``publishing_service`` already imports ``webhooks``,
so the helper cannot live there without the reverse import becoming a cycle.
"""
from __future__ import annotations

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


def clip_error(error: str) -> str:
    """A failure message bounded to :data:`MAX_ERROR_CHARS`.

    Applied where the message is written to a row rather than where it is
    raised, so a new adapter — or a new platform behind an existing one —
    cannot forget it.
    """
    if len(error) <= MAX_ERROR_CHARS:
        return error
    return error[: MAX_ERROR_CHARS - 1].rstrip() + "…"
