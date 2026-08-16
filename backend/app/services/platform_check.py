"""Will this piece publish where it is going, and will it arrive intact?

Two questions that could previously only be answered by trying, and whose
answers arrive too late when you do. The first is a wasted attempt and a
``failed`` row; the second is a live post that is not the post that was on
screen, and on the short-form destinations that has no undo.

**Three kinds of finding, from three different places.** The order matters,
because they are not equally recoverable:

* **The connection.** No live connection for this platform is the single most
  common reason a piece fails on this install — every one of the ten ``failed``
  pieces on production got there that way, before
  :func:`app.services.content_pipeline.publishable_destinations` learned to
  drop a destination its owner never connected. The sentence used here is
  :func:`app.services.publishing_service.not_connected_error`'s, deliberately:
  it is the sentence the row would have carried, the one
  :mod:`app.services.publish_recovery` matches on, and having a fourth spelling
  of it in the tree is how those two come apart.
* **The adapter.** A platform Herald has scaffolded but not finished cannot
  publish anything, and says so here rather than at the end of a queue.
* **The format.** :meth:`app.services.publishers.base.Adapter.preflight` — what
  300 graphemes will do to an article, which tag Forem will drop, whether there
  is a slug to name a file after.

**Reports, never refuses.** Nothing in Herald consults this before publishing,
and that is the design rather than an omission. The adapters already enforce
their own limits — Bluesky composes to 300 characters whatever it is handed —
so a gate here would be a second implementation of those rules, free to
disagree with the first and certain to eventually. This is a read surface: it
tells a person what is about to happen while they can still change it.

**No credentials, no network.** Every check reads the piece, the adapter's
constants and the connection's *status* — never its secrets, and never the
platform. So it is safe on a draft, safe on a platform that is not connected,
and cheap enough to run for every destination at once behind a preview panel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import publishers, publishing_service
from app.services.publishers.base import (
    PREFLIGHT_ERROR,
    PREFLIGHT_WARNING,
    PreflightFinding,
)

logger = logging.getLogger(__name__)


@dataclass
class PlatformVerdict:
    """Everything known about this piece on one platform."""

    platform: Platform
    findings: list[PreflightFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[PreflightFinding]:
        """The findings that stop this piece reaching this platform at all."""
        return [f for f in self.findings if f.is_error]

    @property
    def warnings(self) -> list[PreflightFinding]:
        """The findings the piece survives — it publishes, but not intact."""
        return [f for f in self.findings if not f.is_error]

    @property
    def publishable(self) -> bool:
        """Whether an attempt at this platform is worth making.

        Warnings do not count against it. A post that will be shortened is a
        post that will publish, and treating "this is 400 characters over" as a
        refusal would block the destination whose entire job is to carry a
        short version of a long piece.
        """
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """The verdict as JSON: the platform, the answer, and every reason for it.

        Errors and warnings go out in one ``findings`` list rather than two,
        because the order they were found in is the order they are worth
        reading, and each one already carries its own level.
        """
        return {
            "platform": self.platform.value,
            "publishable": self.publishable,
            "findings": [f.as_dict() for f in self.findings],
        }


def _connected_platforms(db: Session, user_id: int) -> set[Platform]:
    """Which platforms this account has live credentials for, in one query."""
    rows = db.scalars(
        select(PlatformConnection.platform).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.status == ConnectionStatus.CONNECTED,
        )
    )
    return set(rows)


def check(
    db: Session,
    content: Content,
    platforms: list[Platform],
    *,
    user_id: int,
    as_draft: bool = False,
) -> list[PlatformVerdict]:
    """Verdicts for *content* on each of *platforms*, in the order given.

    *as_draft* is passed through to the adapters because it changes the answer:
    Bluesky has no draft state and refuses one, so "publish this as a draft
    everywhere" is a preflight error there and nowhere else.

    The connection lookup is one query for the whole batch rather than one per
    platform — this runs behind a panel that asks about every destination at
    once, and the per-platform version was six round trips to answer one
    screen.
    """
    live = _connected_platforms(db, user_id)
    out: list[PlatformVerdict] = []

    for platform in platforms:
        verdict = PlatformVerdict(platform=platform)
        adapter = publishers.get_adapter(platform)

        if not adapter.implemented:
            verdict.findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    f"Herald cannot publish to {adapter.display_name} yet.",
                )
            )
        elif platform not in live:
            # The publish path's own sentence, not a new one. See the module
            # docstring: `publish_recovery` matches this string to find the rows
            # a new connection un-blocks, so a second wording of it here would
            # be a second thing to keep in step.
            verdict.findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    publishing_service.not_connected_error(platform),
                )
            )

        # Asked even when the platform is unusable. A piece bound for a
        # destination nobody has connected yet still has a format, and the
        # person about to connect it should find out that the excerpt is 400
        # characters too long in the same breath — not on the next screen.
        request = publishing_service.build_request(
            content, platform=platform, as_draft=as_draft
        )
        try:
            verdict.findings.extend(adapter.preflight(request))
        except Exception:
            # A preflight is advisory, and an adapter that raises inside one has
            # a bug in the *advice*, not in the publish path. Losing the panel
            # for every platform because one of them miscounted a limit would
            # make this less useful than having nothing.
            logger.exception(
                "preflight for %s raised on content %s", platform.value, content.id
            )

        out.append(verdict)

    return out


def destinations_for(content: Content) -> list[Platform]:
    """Where this piece is actually going, when the caller does not say.

    The publications it already has, then the project's autopilot destinations
    for the ones it has not been queued for yet — which together are the set a
    person looking at this piece would call "where it publishes". Ordered, and
    deduplicated, so the panel does not show Dev.to twice for a piece that is
    queued there and configured for it.
    """
    out: list[Platform] = []
    seen: set[Platform] = set()

    for publication in content.publications:
        if publication.platform not in seen:
            seen.add(publication.platform)
            out.append(publication.platform)

    project = content.project
    for raw in (project.autopilot_platforms if project else []) or []:
        try:
            platform = raw if isinstance(raw, Platform) else Platform(raw)
        except ValueError:
            # A platform name stored before it was renamed or removed. The
            # projects page is where that gets fixed; a preview panel that 500s
            # on it helps nobody.
            logger.warning("project %s names unknown platform %r", project.id, raw)
            continue
        if platform not in seen:
            seen.add(platform)
            out.append(platform)

    return out


__all__ = [
    "PREFLIGHT_ERROR",
    "PREFLIGHT_WARNING",
    "PlatformVerdict",
    "check",
    "destinations_for",
]
