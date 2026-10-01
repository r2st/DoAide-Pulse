"""The normalized thing a trigger produces: "here is what happened".

Before this module the content engine took a ``RepoActivity`` — commits, a
release, a star count. Everything downstream of a trigger therefore knew it was
looking at GitHub, which is exactly the coupling that made a second trigger kind
impossible to add without a second content engine.

A :class:`TriggerSignal` is what all four kinds collapse to: a headline, some
prose, a list of bullet points, a link, and a key that says whether Pulse has
already seen this. A release, a feed entry, a webhook body and a Friday morning
all fit that shape, and the generator does not need to know which one it got.

The digest this produces is prompt text, so the ordering matters: the highest
signal item goes first, and the bullet list is capped. A model handed a hundred
lines writes about the noise at the end of the list rather than the news at the
top.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.models.content import ContentType
from app.models.trigger import TriggerKind

if TYPE_CHECKING:
    # Type-only, so the runtime decoupling this module exists for survives:
    # nothing downstream of a trigger should import GitHub's client to use a
    # signal. ``from_repo_activity`` is the one adapter that names the type,
    # and naming it in an annotation costs no import.
    from app.services.github_client import RepoActivity

#: How many bullet lines reach the prompt. See the module docstring.
DEFAULT_MAX_ITEMS = 25

#: How much of a free-text summary is worth keeping. Release notes and feed
#: bodies are the highest-signal input there is, so this is generous — but a
#: 40 KB changelog would crowd out every other fact in the brief.
SUMMARY_LIMIT = 3000


@dataclass(frozen=True)
class TriggerSignal:
    """One thing that happened, in the only shape the generator understands."""

    kind: TriggerKind
    #: Where it came from, for the log and the activity list: "GitHub
    #: r2st/Herald", "RSS Changelog", "Webhook from Linear".
    source: str
    #: The one-line description of the event itself — not the title of the post
    #: that will be written about it.
    headline: str
    #: Free prose: release notes, a feed entry's body, a webhook's message.
    summary: str = ""
    #: Bullet points: commit subjects, feed entry titles, list items from a
    #: webhook payload.
    items: tuple[str, ...] = ()
    #: What one item *is*, for the line that introduces the list. "40 new
    #: commit(s)" tells a model something that "40 new item(s)" does not.
    item_noun: str = "item"
    #: Where a reader could go to see the thing itself.
    url: str | None = None
    #: What makes this firing unique. ``None`` when nothing does — a schedule
    #: tick is new every time, and pretending otherwise would fire it once ever.
    dedupe_key: str | None = None
    #: What kind of piece this event deserves, before the project's own
    #: preferences are applied.
    suggested_type: ContentType = ContentType.FEATURE_SPOTLIGHT
    #: The untouched source payload, kept on the event row for debugging.
    raw: dict = field(default_factory=dict)

    @property
    def has_news(self) -> bool:
        """Whether there is anything here worth handing a model."""
        return bool(self.headline.strip() or self.summary.strip() or self.items)

    def digest(self, *, max_items: int = DEFAULT_MAX_ITEMS) -> str:
        """Prompt-ready text for this signal, or ``""`` when it is empty."""
        if not self.has_news:
            return ""

        lines: list[str] = []
        if self.headline.strip():
            lines.append(self.headline.strip())
        if self.summary.strip():
            lines.append(self.summary.strip()[:SUMMARY_LIMIT])

        items = [item.strip() for item in self.items if item and item.strip()]
        if items:
            shown = items[:max_items]
            truncated = (
                f" (showing the {len(shown)} most recent)" if len(shown) < len(items) else ""
            )
            lines.append(f"{len(items)} new {self.item_noun}(s){truncated}:")
            lines.extend(f"- {item}" for item in shown)

        if self.url:
            lines.append(f"Source: {self.url}")

        return "\n".join(lines)

    def event_payload(self) -> dict:
        """The signal as it is stored on a :class:`TriggerEvent` row."""
        return {
            "kind": self.kind.value,
            "source": self.source,
            "headline": self.headline,
            "summary": self.summary[:SUMMARY_LIMIT],
            "items": list(self.items[:DEFAULT_MAX_ITEMS]),
            "item_noun": self.item_noun,
            "url": self.url,
            "dedupe_key": self.dedupe_key,
            "suggested_type": self.suggested_type.value,
            "raw": self.raw,
        }


def digest_key(*parts: str) -> str:
    """A short, stable dedupe key from whatever identifies a firing.

    Hashed rather than concatenated because the inputs are other people's
    identifiers: a feed guid can be a 400-character URL, and the column that
    enforces uniqueness has to hold it.
    """
    joined = "\x1f".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


def from_repo_activity(
    activity: RepoActivity, *, source: str | None = None
) -> TriggerSignal:
    """Adapt a :class:`~app.services.github_client.RepoActivity` to a signal.

    Keeps the editorial judgement that was previously in the autopilot: a
    release is an announcement, because that is what a release *is*; a run of
    ordinary commits is a feature spotlight, because "here is what we've been
    building" reads better than an announcement with nothing to announce.
    """
    release = activity.new_release
    commits = list(activity.new_commits or [])

    if release:
        headline = f"New release {release.tag} — {release.name}".rstrip(" —")
        summary = release.body or ""
        url = release.url or None
        suggested = ContentType.ANNOUNCEMENT
        key = digest_key("release", activity.full_name, release.tag)
    elif commits:
        headline = f"{len(commits)} new commit(s) in {activity.full_name}"
        summary = ""
        url = commits[0].url or None
        suggested = ContentType.FEATURE_SPOTLIGHT
        # Keyed on the newest sha: the same head scanned twice is the same news.
        key = digest_key("commits", activity.full_name, commits[0].sha)
    else:
        headline = ""
        summary = ""
        url = None
        suggested = ContentType.FEATURE_SPOTLIGHT
        key = None

    return TriggerSignal(
        kind=TriggerKind.GITHUB,
        source=source or f"GitHub {activity.full_name}",
        headline=headline,
        summary=summary,
        items=tuple(c.summary for c in commits if c.summary),
        item_noun="commit",
        url=url,
        dedupe_key=key,
        suggested_type=suggested,
        raw={
            "full_name": activity.full_name,
            "release_tag": release.tag if release else None,
            "commit_count": len(commits),
        },
    )


__all__ = [
    "DEFAULT_MAX_ITEMS",
    "SUMMARY_LIMIT",
    "TriggerSignal",
    "digest_key",
    "from_repo_activity",
]
