#!/usr/bin/env python3
"""An MCP server for Herald.

Exposes Herald's HTTP API as MCP tools so an assistant can drive the whole
content workflow — register a project, write a piece, check its links, look at
the calendar, schedule it, read the analytics — without a browser.

It talks to Herald over the same public HTTP API a script would use, rather
than importing the app. That keeps it deployable anywhere, lets it point at
production from a laptop, and means it cannot accidentally reach past the
API's own authorisation checks.

Run it::

    export HERALD_EMAIL=you@example.com
    export HERALD_PASSWORD=...
    export HERALD_BASE_URL=https://herald.aiknol.com/api/v1   # optional
    python mcp/herald_mcp.py

Register it with Claude Code::

    claude mcp add herald -- /path/to/python /path/to/Herald/mcp/herald_mcp.py

**On publishing.** ``herald_publish`` puts a post on a real public account and
cannot be taken back — a Bluesky post is live the moment it is accepted, and a
Buttondown send is an email that cannot be unsent. It is exposed because
controlling Herald through MCP is the point, but it is the one tool here that
does something irreversible in public, and its description says so. Scheduling
is the gentler path: it lands on the calendar, and ``herald_unschedule`` takes
it back off.
"""
from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

_F = TypeVar("_F", bound=Callable[..., Any])

# The SDK renamed the server class in 2.0: ``FastMCP`` became ``MCPServer``,
# and ``mcp.server.fastmcp`` stopped existing. The decorator and ``run()``
# surfaces this file uses are the same on both, so supporting each is one
# import rather than a compatibility layer.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # SDK >= 2.0
except ImportError:  # pragma: no cover - exercised by whichever SDK is absent
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # SDK 1.x
    except ImportError:
        sys.exit(
            "The MCP SDK is not installed. Run:\n"
            "    pip install -r mcp/requirements.txt"
        )

# The API client lives with the campaign runner; both speak to the same API and
# there is no reason to have two copies of the contract.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "marketing"))

from herald_client import DEFAULT_BASE_URL, HeraldClient, HeraldError  # noqa: E402

mcp = _Server("herald")

_client: HeraldClient | None = None


def client() -> HeraldClient:
    """The logged-in client, created on first use.

    Lazy because a server that dies at import time when credentials are absent
    is much harder to diagnose from an MCP host than one that returns a clear
    error from the first tool call.
    """
    global _client
    if _client is None:
        base_url = os.environ.get("HERALD_BASE_URL", DEFAULT_BASE_URL)
        candidate = HeraldClient(base_url)
        candidate.login_from_env()
        _client = candidate
    return _client


def _guard(fn: _F) -> _F:
    """Turn a HeraldError into a readable message rather than a traceback.

    Herald's ``detail`` strings are written to be actionable — "Not connected
    to: devto. Add credentials in Settings." is the fix — and an MCP host shows
    the returned text to the model, so passing it through is worth more than
    the stack trace.

    ``functools.wraps`` is load-bearing, not tidiness. The SDK builds each
    tool's input schema by introspecting the function signature, and a bare
    ``*args, **kwargs`` wrapper advertises every tool as taking two arguments
    called ``args`` and ``kwargs`` — which validates against nothing a caller
    would ever send. ``wraps`` sets ``__wrapped__``, and ``inspect.signature``
    follows it back to the real parameters.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HeraldError as exc:
            return {"error": exc.detail, "status_code": exc.status_code}

    return cast(_F, wrapper)


# --------------------------------------------------------------------------- #
# Projects                                                                     #
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def herald_list_projects() -> list[dict]:
    """List every project, with its slug, autopilot settings and content counts."""
    return client().list_projects()


@mcp.tool()
@_guard
def herald_create_project(
    name: str,
    description: str = "",
    repo_url: str | None = None,
    live_url: str | None = None,
    tech_stack: list[str] | None = None,
    target_audience: str = "",
    keywords: list[str] | None = None,
    tone: str = "technical",
    canonical_platform: str | None = None,
) -> dict:
    """Register a project for Herald to write about.

    ``tone`` is one of technical, casual, marketing.

    ``canonical_platform`` names the destination that counts as the original
    when a piece goes to several. Set it to the platform you want search to
    rank — Herald then adopts that URL as the piece's canonical, and every
    other copy points back to it instead of competing with it.
    """
    return client().create_project(
        {
            "name": name,
            "description": description,
            "repo_url": repo_url,
            "live_url": live_url,
            "tech_stack": tech_stack or [],
            "target_audience": target_audience,
            "keywords": keywords or [],
            "tone": tone,
            "canonical_platform": canonical_platform,
        }
    )


@mcp.tool()
@_guard
def herald_update_project(project_id: int, changes: dict) -> dict:
    """Patch a project. Only the keys present in ``changes`` are touched."""
    return client().update_project(project_id, changes)


@mcp.tool()
@_guard
def herald_scan_project(project_id: int) -> dict:
    """Pull new commits and releases for a project's repo since the last scan."""
    return client()._request("POST", f"/projects/{project_id}/scan")


# --------------------------------------------------------------------------- #
# Content                                                                      #
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def herald_list_content(project_id: int | None = None, status: str | None = None) -> list[dict]:
    """List content, optionally filtered by project or status.

    Statuses are draft, review, approved, published, archived, failed. Bodies
    are omitted — use ``herald_get_content`` for one piece's markdown.
    """
    params: dict[str, Any] = {"limit": 200}
    if project_id is not None:
        params["project_id"] = project_id
    if status is not None:
        params["status"] = status
    return client()._request("GET", "/content", params=params)


@mcp.tool()
@_guard
def herald_get_content(content_id: int) -> dict:
    """One piece in full: body markdown, SEO issues and format issues."""
    return client().get_content(content_id)


@mcp.tool()
@_guard
def herald_create_content(
    project_id: int,
    title: str,
    body_markdown: str,
    content_type: str = "tutorial",
    excerpt: str = "",
    meta_description: str = "",
    keywords: list[str] | None = None,
    tags: list[str] | None = None,
    focus_keyword: str = "",
    campaign_key: str | None = None,
) -> dict:
    """Write a piece by hand. Lands as a draft; nothing is published.

    ``content_type`` is one of tutorial, announcement, feature_spotlight,
    comparison, how_to, social_thread, changelog. The first five are articles;
    the last two have their own shape and are validated against it.

    ``campaign_key`` is an optional stable identifier of your own, stored on
    the piece. Set one if you intend to find this content again later — the
    title is not a reliable handle, because editing it is exactly what an
    editing pass does.
    """
    return client().create_content(
        {
            "project_id": project_id,
            "title": title,
            "body_markdown": body_markdown,
            "content_type": content_type,
            "excerpt": excerpt,
            "meta_description": meta_description,
            "keywords": keywords or [],
            "tags": tags or [],
            "focus_keyword": focus_keyword,
            "campaign_key": campaign_key,
        }
    )


@mcp.tool()
@_guard
def herald_generate_content(
    project_id: int,
    content_type: str = "feature_spotlight",
    instructions: str = "",
    include_repo_activity: bool = False,
) -> dict:
    """Draft a piece with Herald's AI engine. Runs inline; allow ~20 seconds.

    ``instructions`` steers it ("focus on the retry logic").
    ``include_repo_activity`` pulls the latest commits and releases first,
    which is what makes a release-driven post specific rather than generic.
    """
    return client().generate_content(
        {
            "project_id": project_id,
            "content_type": content_type,
            "instructions": instructions,
            "include_repo_activity": include_repo_activity,
        }
    )


@mcp.tool()
@_guard
def herald_update_content(content_id: int, changes: dict) -> dict:
    """Patch a piece — title, body_markdown, keywords, tags, status, and so on.

    Herald refuses edits to an already-published piece beyond status and
    schedule, because changing the row would not change what is live on the
    platforms.
    """
    return client().update_content(content_id, changes)


@mcp.tool()
@_guard
def herald_approve_content(content_id: int) -> dict:
    """Mark a piece approved, releasing anything already queued for it."""
    return client().approve_content(content_id)


@mcp.tool()
@_guard
def herald_check_links(content_id: int) -> dict:
    """Check every URL in the body. Worth running before scheduling anything.

    A definitive 404 blocks publishing; a timeout does not, and is reported as
    "unknown" rather than treated as broken.
    """
    return client().check_links(content_id)


@mcp.tool()
@_guard
def herald_social_preview(content_id: int) -> dict:
    """How the link card will render per network, plus the meta tags for it."""
    return client().social_cards(content_id)


@mcp.tool()
@_guard
def herald_repurpose(content_id: int) -> dict:
    """Derive social snippets from a long piece. Returns them; persists nothing."""
    return client()._request("POST", f"/content/{content_id}/repurpose")


@mcp.tool()
@_guard
def herald_headline_variants(content_id: int) -> dict:
    """Suggest alternative headlines for a piece. Nothing is applied."""
    return client()._request("POST", f"/content/{content_id}/headlines")


# --------------------------------------------------------------------------- #
# Scheduling and publishing                                                    #
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def herald_platforms() -> list[dict]:
    """Every destination, whether an adapter exists, and whether it is connected.

    Check this before scheduling: Herald rejects a publish to a platform with
    no live connection, and the rejection is the same whether the credentials
    were never added or have gone stale.
    """
    return client().platforms()


@mcp.tool()
@_guard
def herald_schedule_suggestions(content_id: int, platforms: list[str]) -> list[dict]:
    """When Herald would put this out, and why. Nothing is queued or changed."""
    return client().schedule_suggestions(content_id, platforms)


@mcp.tool()
@_guard
def herald_schedule(
    content_id: int,
    platforms: list[str] | None = None,
    scheduled_for: str | None = None,
    optimize: bool = False,
    as_draft: bool = False,
) -> list[dict]:
    """Put a piece on the calendar.

    Give either ``scheduled_for`` (ISO 8601, must be in the future) or
    ``optimize``, not both. With ``optimize`` Herald picks a slot per platform
    from its cadence table, which staggers a cross-post rather than firing
    every copy into every feed in the same second.

    This arranges for the post to go out unattended at that time. Use
    ``herald_unschedule`` to take it back off before it fires.
    """
    return client().schedule_content(
        content_id,
        platforms=platforms,
        scheduled_for=scheduled_for,
        optimize=optimize,
        as_draft=as_draft,
    )


@mcp.tool()
@_guard
def herald_unschedule(content_id: int) -> list[dict]:
    """Take a piece off the calendar. Anything already published stays published."""
    return client().unschedule_content(content_id)


@mcp.tool()
@_guard
def herald_publish(
    content_id: int,
    platforms: list[str],
    as_draft: bool = False,
    allow_broken_links: bool = False,
) -> list[dict]:
    """Publish now, to real public accounts. This cannot be undone.

    A Bluesky post is live the moment it is accepted and has no draft state; a
    Buttondown publish sends the newsletter to real subscribers and an email
    cannot be unsent. Confirm with the person you are working for before
    calling this — and prefer ``as_draft`` on the platforms that support it, or
    ``herald_schedule``, which is reversible right up until it fires.

    The response is the queue state, not the outcome. Poll
    ``herald_get_content`` to see what each platform said.
    """
    return client().publish_content(
        content_id,
        platforms,
        as_draft=as_draft,
        allow_broken_links=allow_broken_links,
    )


# --------------------------------------------------------------------------- #
# Calendar and analytics                                                       #
# --------------------------------------------------------------------------- #


@mcp.tool()
@_guard
def herald_calendar(start: str | None = None, end: str | None = None) -> dict:
    """Scheduled and published items in a window, with cadence guidance."""
    return client().calendar(start, end)


@mcp.tool()
@_guard
def herald_analytics() -> dict:
    """The dashboard rollup: views, engagement, trends and alerts."""
    return client().analytics_dashboard()


@mcp.tool()
@_guard
def herald_review_queue() -> list[dict]:
    """Everything waiting for a human decision before it can go out."""
    return client()._request("GET", "/content/queue/review")


if __name__ == "__main__":
    mcp.run()
