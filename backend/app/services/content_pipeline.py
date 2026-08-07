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
from app.models.mixins import as_aware, utcnow
from app.models.project import AutopilotMode, Project
from app.models.publication import Platform, Publication
from app.models.webhook import WebhookEvent
from app.services import (
    content_generator,
    link_check,
    publishers,
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
    destinations = _publishable_destinations(project)
    auto = bool(mode == AutopilotMode.AUTO and confident and destinations)

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

    publications = publishing_service.queue(db, content, destinations)
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


def release_approved(db: Session, content: Content) -> list[Publication]:
    """Queue an approved piece for its project's autopilot destinations.

    This is what makes the review queue a *queue* rather than a filing cabinet.
    A project on ``auto`` publishes what the autopilot writes without asking;
    the two quality gates above (dead links, SEO score) divert a piece to review
    instead, and a human clicking Approve is that piece clearing the gate. Before
    this, approving set a column and stopped: nothing queued a publication and no
    sweep looked for approved content, so five pieces sat ``approved`` on the box
    for days while the beat swept a publications table that had no rows for them.

    Deliberately narrow. It fires only when:

    * the piece is ``approved`` — a draft is not waiting on anything, and a
      published or failed one is finished;
    * nothing has been queued for it yet. A publication row of any status means a
      destination was already chosen, by a human or by an earlier pass, and this
      must not add to it or re-arm a row somebody cancelled;
    * the project is active, its owner's account is active, and it is on
      ``auto`` with destinations configured. ``off`` and ``draft`` mean "do not
      publish without me", and approving is not the same as saying where to.

    A piece the user has already dated keeps that date: ``content.scheduled_for``
    in the future is a decision about when this goes out, and approving it is
    saying yes to the piece, not moving it to now.

    Returns the publications it queued — empty whenever any of those does not
    hold, which is the common case. Commits when it queues, and dispatches the
    rows whose time has come.
    """
    if content.status != ContentStatus.APPROVED:
        return []
    if content.publications:
        return []

    project = content.project
    if project is None or not project.is_active:
        return []
    if project.user is None or not project.user.is_active:
        return []
    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    if mode != AutopilotMode.AUTO:
        return []

    platforms = _publishable_destinations(project)
    if not platforms:
        return []

    when = as_aware(content.scheduled_for) if content.scheduled_for else None
    if when is not None and when <= utcnow():
        # A date that has already passed is not a schedule any more. Queue it
        # for now rather than for the past, which reads the same to the sweep
        # and worse in the UI.
        when = None

    publications = publishing_service.queue(db, content, platforms, scheduled_for=when)
    db.commit()
    for publication in publications:
        if publication.scheduled_for is None:
            publish_now(publication.id)
    logger.info(
        "released approved content %s to %s",
        content.id,
        ", ".join(p.platform.value for p in publications),
    )
    return publications


def _publishable_destinations(project: Project) -> list[Platform]:
    """The autopilot destinations Herald can actually post to.

    ``autopilot_platforms`` is a plain JSON column. Values written before the
    schema validators existed can name a platform with no finished adapter, and
    rows written through the ORM's enum machinery spell the name in upper case
    (see :class:`app.models.publication.Platform`). Both are read here rather
    than trusted: an unknown string would raise out of a beat sweep, and an
    unfinished adapter fails the publication terminally, which drives the piece
    to ``failed`` instead of leaving it approved.
    """
    out: list[Platform] = []
    for raw in project.autopilot_platforms or []:
        try:
            platform = raw if isinstance(raw, Platform) else Platform(raw)
        except ValueError:
            logger.warning(
                "project %s names an unknown autopilot destination %r", project.id, raw
            )
            continue
        if not publishers.get_adapter(platform).implemented:
            logger.warning(
                "project %s names %s, which has no finished adapter",
                project.id,
                platform.value,
            )
            continue
        if platform not in out:
            out.append(platform)
    return out


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
    "release_approved",
]
