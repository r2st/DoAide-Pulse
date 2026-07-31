"""Auto-pilot: watch the repos, write when something ships.

The loop for one project is:

1. Ask GitHub what is new since the stored watermark.
2. Decide whether it is *worth* writing about (a release always is; loose
   commits only past a threshold).
3. Generate a piece, and bank an idea for later either way.
4. Route it — straight to publish if the project says ``auto`` and the model is
   confident, otherwise into the review queue.
5. Move the watermark, so the same commits never trigger twice.

Step 5 happens **whatever the outcome**, including when generation falls back to
a template. A watermark that only advances on success means one bad scan makes
every subsequent scan re-report the same backlog, and the daily cap then burns
itself on the same commits every day.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Project, slugify
from app.services import (
    content_generator,
    github_client,
    link_check,
    publishing_service,
    seo,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _pick_content_type(activity: github_client.RepoActivity) -> ContentType:
    """What kind of piece this change deserves.

    A release is an announcement — that is what a release *is*. A run of
    ordinary commits is a feature spotlight, because "here is what we've been
    building" reads better than an announcement with nothing to announce.
    """
    if activity.new_release:
        return ContentType.ANNOUNCEMENT
    return ContentType.FEATURE_SPOTLIGHT


def _worth_writing(activity: github_client.RepoActivity) -> bool:
    """Is there enough here to justify a post?

    A release always is. Loose commits need to clear a threshold: writing an
    announcement about three typo fixes is how an audience learns to ignore you.
    """
    if activity.new_release:
        return True
    return len(activity.new_commits) >= settings.autopilot_commit_threshold


def _daily_count(db: Session, project_id: int) -> int:
    since = utcnow() - timedelta(days=1)
    return (
        db.scalar(
            select(func.count(Content.id)).where(
                Content.project_id == project_id,
                Content.created_at >= since,
                Content.source["kind"].as_string() == "autopilot",
            )
        )
        or 0
    )


def _unique_slug(db: Session, project_id: int, title: str) -> str:
    base = slugify(title)
    candidate, suffix = base, 2
    while db.scalar(
        select(Content.id).where(
            Content.project_id == project_id, Content.slug == candidate
        )
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


@celery_app.task(name="app.tasks.autopilot_tasks.scan_project")
def scan_project(project_id: int) -> dict:
    """Run the autopilot loop for one project. Never raises."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None or not project.is_active:
            return {"project_id": project_id, "status": "skipped"}

        full_name = project.repo_full_name
        if not full_name:
            return {"project_id": project_id, "status": "no_repo"}

        try:
            activity = github_client.fetch_activity(
                full_name,
                since_sha=project.last_seen_commit_sha,
                since_tag=project.last_seen_release_tag,
            )
        except github_client.GitHubRateLimited as exc:
            # Do not move the watermark — we genuinely did not look.
            logger.warning("autopilot rate-limited on %s: %s", full_name, exc)
            return {"project_id": project_id, "status": "rate_limited"}
        except github_client.GitHubError as exc:
            logger.warning("autopilot could not read %s: %s", full_name, exc)
            project.last_scanned_at = utcnow()
            db.commit()
            return {"project_id": project_id, "status": "unreachable"}

        # First-ever scan: record where the repo is and write nothing. Otherwise
        # registering a five-year-old project produces a post about five years
        # of history.
        first_scan = project.last_seen_commit_sha is None
        result = _act_on(db, project, activity, first_scan=first_scan)

        project.last_seen_commit_sha = activity.head_sha
        project.last_seen_release_tag = activity.latest_tag
        project.last_scanned_at = utcnow()
        db.commit()
        return {"project_id": project_id, **result}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("autopilot crashed on project %s: %s", project_id, exc)
        return {"project_id": project_id, "status": "error", "error": str(exc)}
    finally:
        db.close()


def _act_on(
    db: Session, project: Project, activity: github_client.RepoActivity, *, first_scan: bool
) -> dict:
    """Decide and do, for one scanned project."""
    if first_scan:
        return {"status": "baselined", "commits": len(activity.new_commits)}
    if not activity.has_news:
        return {"status": "no_news"}

    # Bank an idea regardless of whether we write now: the calendar's
    # "suggested" column is fed from these, and an idea costs nothing to keep.
    for idea in content_generator.suggest_ideas(project, activity=activity, limit=2):
        db.add(
            ContentIdea(
                project_id=project.id,
                content_type=idea.content_type,
                headline=idea.headline,
                rationale=idea.rationale,
                source={"kind": "autopilot", "commits": len(activity.new_commits)},
            )
        )

    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    if mode == AutopilotMode.OFF:
        return {"status": "ideas_only"}
    if not _worth_writing(activity):
        return {"status": "below_threshold", "commits": len(activity.new_commits)}
    if _daily_count(db, project.id) >= settings.autopilot_daily_content_limit:
        return {"status": "daily_limit_reached"}

    content_type = _pick_content_type(activity)
    generated = content_generator.generate(project, content_type, activity=activity)

    confident = generated.confidence >= settings.autopilot_auto_publish_confidence
    auto = bool(mode == AutopilotMode.AUTO and confident and project.autopilot_platforms)

    # An unreviewed publish is the one place a fabricated URL reaches an audience
    # unchallenged, so it is the one place worth spending the requests. A dead
    # link is not a failed generation — the piece is fine and one link is wrong —
    # so it goes to review rather than being discarded.
    dead_links: list[str] = []
    if auto and settings.link_check_enabled:
        dead_links = [
            s.url
            for s in link_check.broken(link_check.check_body(generated.body_markdown))
        ]
        if dead_links:
            auto = False
            logger.info(
                "autopilot held %r back from auto-publish: %d dead link(s): %s",
                generated.title,
                len(dead_links),
                ", ".join(dead_links),
            )

    # SEO quality gate: a post that would rank poorly should not go out
    # unreviewed, even if the model is confident about accuracy.
    score = seo.seo_score(
        title=generated.title,
        body_markdown=generated.body_markdown,
        meta_description=generated.meta_description,
        keywords=generated.keywords,
        focus_keyword=generated.focus_keyword,
        slug=_unique_slug(db, project.id, generated.title),
        cover_image_url=None,  # autopilot rarely has one
    )
    if auto and score < seo.SEO_SCORE_THRESHOLD:
        auto = False
        logger.info(
            "autopilot held %r back from auto-publish: SEO score %d < %d",
            generated.title,
            score,
            seo.SEO_SCORE_THRESHOLD,
        )

    content = Content(
        project_id=project.id,
        content_type=content_type,
        title=generated.title,
        slug=_unique_slug(db, project.id, generated.title),
        body_markdown=generated.body_markdown,
        excerpt=generated.excerpt,
        meta_description=generated.meta_description,
        keywords=generated.keywords,
        tags=generated.tags,
        confidence=generated.confidence,
        generated_by_provider=generated.provider,
        generated_by_model=generated.model,
        source={
            "kind": "autopilot",
            "trigger": "release" if activity.new_release else "commits",
            "release_tag": activity.new_release.tag if activity.new_release else None,
            "commit_count": len(activity.new_commits),
            "fallback": generated.is_fallback,
            # Recorded so the review queue can say *why* a confident piece is
            # sitting there instead of having gone out.
            "dead_links": dead_links,
            "seo_score": score,
        },
        focus_keyword=generated.focus_keyword,
    )

    content.status = ContentStatus.APPROVED if auto else ContentStatus.REVIEW
    db.add(content)
    db.flush()

    if not auto:
        db.commit()
        return {
            "status": "queued_for_review",
            "content_id": content.id,
            "confidence": generated.confidence,
            "dead_links": dead_links,
        }

    publications = publishing_service.queue(db, content, list(project.autopilot_platforms))
    db.commit()
    for publication in publications:
        _publish_now(publication.id)

    return {
        "status": "auto_published",
        "content_id": content.id,
        "platforms": [p.platform.value for p in publications],
    }


def _publish_now(publication_id: int) -> None:
    """Hand a publication to a worker, or do it here if the broker is down."""
    from app.tasks import publish_tasks

    if settings.celery_enabled:
        try:
            publish_tasks.publish_one.delay(publication_id)
            return
        except Exception as exc:  # pragma: no cover - broker down
            logger.warning("autopilot dispatch failed, publishing inline: %s", exc)
    publish_tasks.publish_one(publication_id)


@celery_app.task(name="app.tasks.autopilot_tasks.scan_all_projects")
def scan_all_projects() -> dict:
    """Beat task: scan every active project that has a repo."""
    db = SessionLocal()
    try:
        ids = list(
            db.scalars(
                select(Project.id).where(
                    Project.is_active.is_(True), Project.repo_url.is_not(None)
                )
            )
        )
    finally:
        db.close()

    # Dispatch each project as a separate Celery task so they run in parallel
    # across workers instead of blocking a single task for the entire fleet.
    dispatched = 0
    for project_id in ids:
        try:
            scan_project.delay(project_id)
            dispatched += 1
        except Exception:
            # Broker down — fall back to inline.
            scan_project(project_id)
            dispatched += 1

    logger.info("autopilot scanned %d project(s), wrote %d", len(ids), dispatched)
    return {"scanned": len(ids), "written": dispatched}
