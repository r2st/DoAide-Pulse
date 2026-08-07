"""Generate a piece from a signal, then decide where it goes.

This is the half of the autopilot that has nothing to do with GitHub: write the
draft, run the two quality gates, and either park it for review or queue it for
publication. It was inlined in ``autopilot_tasks`` when a repo push was the only
thing that could start a piece. Triggers made that a problem — an RSS entry and
a Friday morning deserve exactly the same gates, and a second copy of them would
drift within a release.

The gates only apply to an *unreviewed* publish, which is deliberate:

* **Dead links.** An auto-publish is the one place a fabricated URL reaches an
  audience unchallenged. A dead link is not a failed generation — the piece is
  fine and one link is wrong — so it demotes to review rather than being thrown
  away.
* **SEO score.** A post that would rank poorly should not go out unreviewed even
  when the model is confident it is accurate. Confidence is about truth; the
  score is about whether anyone will find it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType, unique_content_slug
from app.models.project import AutopilotMode, Project
from app.models.webhook import WebhookEvent
from app.services import (
    content_generator,
    link_check,
    publishing_service,
    seo,
    webhook_payloads,
    webhooks,
)
from app.services.signals import TriggerSignal

logger = logging.getLogger(__name__)

#: What ``RoutedContent.status`` can be. Strings rather than an enum because they
#: are a task return value read by a human in a log, not a stored column.
QUEUED_FOR_REVIEW = "queued_for_review"
AUTO_PUBLISHED = "auto_published"


@dataclass
class RoutedContent:
    """A generated piece and what was decided about it."""

    content: Content
    status: str
    confidence: float
    seo_score: int
    dead_links: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    is_fallback: bool = False

    @property
    def auto_published(self) -> bool:
        return self.status == AUTO_PUBLISHED

    def summary(self) -> dict[str, Any]:
        """The dict a Celery task returns for this outcome."""
        body: dict[str, Any] = {
            "status": self.status,
            "content_id": self.content.id,
            "confidence": self.confidence,
            "seo_score": self.seo_score,
        }
        if self.dead_links:
            body["dead_links"] = self.dead_links
        if self.platforms:
            body["platforms"] = self.platforms
        return body


def generate_and_route(
    db: Session,
    project: Project,
    *,
    content_type: ContentType,
    signal: TriggerSignal | None = None,
    activity: Any = None,
    instructions: str = "",
    source: dict[str, Any],
) -> RoutedContent:
    """Write one piece for *project* and route it. Commits.

    *source* is the provenance dict stored on the content row; this adds the
    quality-gate results to it so a reviewer can see why a piece was held back
    without reading the logs.
    """
    generated = content_generator.generate(
        project,
        content_type,
        activity=activity,
        signal=signal,
        instructions=instructions,
    )

    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    confident = generated.confidence >= settings.autopilot_auto_publish_confidence
    auto = bool(
        mode == AutopilotMode.AUTO and confident and project.autopilot_platforms
    )

    dead_links: list[str] = []
    if auto and settings.link_check_enabled:
        dead_links = [
            s.url
            for s in link_check.broken(link_check.check_body(generated.body_markdown))
        ]
        if dead_links:
            auto = False
            logger.info(
                "held %r back from auto-publish: %d dead link(s): %s",
                generated.title,
                len(dead_links),
                ", ".join(dead_links),
            )

    slug = unique_content_slug(db, project.id, generated.title)
    score = seo.seo_score(
        title=generated.title,
        body_markdown=generated.body_markdown,
        meta_description=generated.meta_description,
        keywords=generated.keywords,
        focus_keyword=generated.focus_keyword,
        slug=slug,
        cover_image_url=None,  # an automated piece rarely has one
    )
    if auto and score < seo.SEO_SCORE_THRESHOLD:
        auto = False
        logger.info(
            "held %r back from auto-publish: SEO score %d < %d",
            generated.title,
            score,
            seo.SEO_SCORE_THRESHOLD,
        )

    content = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=content_type,
        generated=generated,
        status=ContentStatus.APPROVED if auto else ContentStatus.REVIEW,
        source={
            **source,
            "fallback": generated.is_fallback,
            "dead_links": dead_links,
            "seo_score": score,
        },
    )
    db.add(content)
    db.flush()

    if not auto:
        db.commit()
        # The review queue is only a queue if somebody knows it has something in
        # it. This is the one moment an automated piece needs a human and cannot
        # ask for one through a UI nobody is looking at.
        webhooks.emit(
            db,
            user_id=project.user_id,
            event=WebhookEvent.REVIEW_PENDING,
            data={
                "content": webhook_payloads.content_payload(content),
                "confidence": generated.confidence,
                "seo_score": score,
                "dead_links": dead_links,
                "review_url": f"{settings.frontend_url.rstrip('/')}/content/{content.id}",
            },
        )
        return RoutedContent(
            content=content,
            status=QUEUED_FOR_REVIEW,
            confidence=generated.confidence,
            seo_score=score,
            dead_links=dead_links,
            is_fallback=generated.is_fallback,
        )

    publications = publishing_service.queue(
        db, content, list(project.autopilot_platforms)
    )
    db.commit()
    # Only the rows whose time has come. A project with a canonical platform and
    # more than one autopilot destination has its syndicated copies parked behind
    # the original by ``publishing_service._syndication_schedule``; the beat sweep
    # picks those up when the delay is up.
    for publication in publications:
        if publication.scheduled_for is None:
            publish_now(publication.id)

    return RoutedContent(
        content=content,
        status=AUTO_PUBLISHED,
        confidence=generated.confidence,
        seo_score=score,
        platforms=[p.platform.value for p in publications],
        is_fallback=generated.is_fallback,
    )


def publish_now(publication_id: int) -> None:
    """Hand a publication to a worker, or do it here if the broker is down."""
    from app.tasks import publish_tasks

    if settings.celery_enabled:
        try:
            publish_tasks.publish_one.delay(publication_id)
            return
        except Exception as exc:  # pragma: no cover - broker down
            logger.warning("dispatch failed, publishing inline: %s", exc)
    publish_tasks.publish_one(publication_id)


__all__ = [
    "AUTO_PUBLISHED",
    "QUEUED_FOR_REVIEW",
    "RoutedContent",
    "generate_and_route",
    "publish_now",
]
