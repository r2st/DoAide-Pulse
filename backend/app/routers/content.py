"""Content: generate, edit, review, approve, publish."""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, owned_project
from app.models.content import Content, ContentIdea, ContentStatus, ContentType, unique_content_slug
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import (
    ContentCreate,
    ContentDetail,
    ContentOut,
    ContentUpdate,
    GenerateRequest,
    LinkCheckOut,
    LinkStatusOut,
    PublicationOut,
    PublishRequestIn,
    SeoIssueOut,
)
from app.services import (
    content_generator,
    github_client,
    link_check,
    publishers,
    publishing_service,
    seo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/content", tags=["content"])


def _owned_content(content_id: int, db: Session, user: User) -> Content:
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found"
        )
    return content


def _to_out(content: Content) -> ContentOut:
    return ContentOut(
        **{
            key: getattr(content, key)
            for key in (
                "id", "project_id", "content_type", "status", "title", "slug",
                "excerpt", "meta_description", "keywords", "tags", "canonical_url",
                "cover_image_url", "focus_keyword",
                "confidence", "generated_by_provider", "generated_by_model",
                "source", "scheduled_for", "published_at", "created_at", "updated_at",
            )
        },
        project_name=content.project.name if content.project else None,
        word_count=content.word_count,
        read_minutes=content.read_minutes,
        publications=[PublicationOut.model_validate(p) for p in content.publications],
    )


def _to_detail(content: Content) -> ContentDetail:
    issues = seo.audit(
        title=content.title,
        body_markdown=content.body_markdown,
        meta_description=content.meta_description,
        keywords=list(content.keywords or []),
        cover_image_url=content.cover_image_url,
        focus_keyword=getattr(content, "focus_keyword", "") or "",
        slug=content.slug,
    )
    return ContentDetail(
        **_to_out(content).model_dump(),
        body_markdown=content.body_markdown,
        seo_issues=[
            SeoIssueOut(level=i.level, field=i.field, message=i.message) for i in issues
        ],
    )


def _commit_content(db: Session, content: Content) -> None:
    """Add and commit, retrying once on a slug collision.

    ``_unique_slug`` prevents most collisions, but two concurrent requests can
    race past the check. The database-level unique constraint catches the
    loser; we regenerate the slug and retry rather than surfacing a 500.
    """
    db.add(content)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        content.slug = f"{content.slug}-{secrets.token_hex(3)}"
        db.add(content)
        db.commit()
    db.refresh(content)


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ContentOut])
def list_content(
    response: Response,
    project_id: int | None = None,
    status_filter: ContentStatus | None = Query(default=None, alias="status"),
    content_type: ContentType | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ContentOut]:
    # Base filter used by both the count and the data query.
    base = (
        select(Content)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id)
    )
    if project_id is not None:
        base = base.where(Content.project_id == project_id)
    if status_filter is not None:
        base = base.where(Content.status == status_filter)
    if content_type is not None:
        base = base.where(Content.content_type == content_type)

    # Total count for the UI to render pagination controls.
    total = db.scalar(
        select(func.count()).select_from(base.with_only_columns(Content.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)

    # ``_to_out`` reads ``content.project.name`` for every row. The join
    # above only filters — it does not populate the relationship — so
    # without this the default lazy load fires one SELECT per row, and this
    # endpoint returns up to 500 of them.
    query = (
        base
        .options(joinedload(Content.project))
        .order_by(Content.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    return [_to_out(c) for c in db.scalars(query)]


# NB: the literal paths under /content (``/queue/...``) are declared here,
# *before* ``GET /{content_id}``. FastAPI matches in declaration order, so a
# ``/{content_id}`` registered first would swallow ``/queue/review`` and 422 on
# "queue" not being an int.


@router.get("/queue/review", response_model=list[ContentOut])
def review_queue(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ContentOut]:
    """Everything the autopilot wrote that is waiting on a human."""
    base = (
        select(Content)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id, Content.status == ContentStatus.REVIEW)
    )
    total = db.scalar(
        select(func.count()).select_from(base.with_only_columns(Content.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)
    rows = db.scalars(
        base.options(joinedload(Content.project))
        .order_by(Content.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_to_out(c) for c in rows]


@router.get("/queue/publications", response_model=list[PublicationOut])
def publication_queue(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Everything in flight or waiting: pending, scheduled, publishing, failed."""
    base = (
        select(Publication)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Publication.status != PublicationStatus.PUBLISHED,
        )
    )
    total = db.scalar(
        select(func.count()).select_from(
            base.with_only_columns(Publication.id).subquery()
        )
    )
    response.headers["X-Total-Count"] = str(total or 0)
    rows = db.scalars(
        base.order_by(
            Publication.scheduled_for.is_(None).desc(), Publication.scheduled_for
        )
        .offset(offset)
        .limit(limit)
    )
    return [PublicationOut.model_validate(p) for p in rows]


@router.get("/{content_id}", response_model=ContentDetail)
def get_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    return _to_detail(_owned_content(content_id, db, user))


@router.get("/{content_id}/links", response_model=LinkCheckOut)
def check_links(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LinkCheckOut:
    """HEAD-check every URL in the body, on demand.

    Not folded into ``GET /{content_id}``: that endpoint is loaded every time the
    editor opens and this one makes up to ``link_check_max_urls`` outbound
    requests. Kept separate so reading a draft stays free.
    """
    content = _owned_content(content_id, db, user)
    return _to_link_check(_check_content_links(content))


def _check_content_links(content: Content) -> list[link_check.LinkStatus]:
    """Every URL this piece would publish, cover image included.

    The cover is the one URL a reader never clicks and always sees, so a dead one
    is a visibly broken card in every feed rather than a 404 nobody reaches.
    """
    return link_check.check_body(
        content.body_markdown,
        extra_urls=[content.cover_image_url] if content.cover_image_url else None,
    )


def _to_link_check(statuses: list[link_check.LinkStatus]) -> LinkCheckOut:
    return LinkCheckOut(
        links=[
            LinkStatusOut(
                url=s.url, status=s.status, http_status=s.http_status, detail=s.detail
            )
            for s in statuses
        ],
        broken_count=len(link_check.broken(statuses)),
        checked=len(statuses),
    )


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #


@router.post("/generate", response_model=ContentDetail, status_code=status.HTTP_201_CREATED)
def generate_content(
    payload: GenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Draft a piece with the AI engine.

    Runs inline rather than on a worker: the user is watching, and a draft that
    arrives in the response is worth the ~20s wait. The autopilot path is the
    one that goes through Celery.
    """
    project = owned_project(payload.project_id, db, user)

    activity = None
    if payload.include_repo_activity and project.repo_full_name:
        try:
            activity = github_client.fetch_activity(
                project.repo_full_name,
                since_sha=project.last_seen_commit_sha,
                since_tag=project.last_seen_release_tag,
            )
        except github_client.GitHubError as exc:
            # A missing changelog is a thinner post, not a failed request.
            logger.info("repo activity unavailable for project %s: %s", project.id, exc)

    generated = content_generator.generate(
        project,
        payload.content_type,
        activity=activity,
        instructions=payload.instructions,
    )

    content = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=payload.content_type,
        generated=generated,
        status=ContentStatus.DRAFT,
        source={
            "kind": "manual",
            "user_id": user.id,
            "instructions": payload.instructions,
            "fallback": generated.is_fallback,
        },
    )
    _commit_content(db, content)
    return _to_detail(content)


@router.post("", response_model=ContentDetail, status_code=status.HTTP_201_CREATED)
def create_content(
    payload: ContentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Write a piece by hand."""
    project = owned_project(payload.project_id, db, user)
    keywords = seo.normalize_keywords(payload.keywords)
    content = Content(
        project_id=project.id,
        content_type=payload.content_type,
        status=ContentStatus.DRAFT,
        title=payload.title,
        slug=unique_content_slug(db, project.id, payload.title),
        body_markdown=payload.body_markdown,
        excerpt=payload.excerpt or seo.build_excerpt(payload.body_markdown),
        meta_description=payload.meta_description
        or seo.build_meta_description("", fallback_body=payload.body_markdown),
        keywords=keywords,
        tags=payload.tags,
        focus_keyword=payload.focus_keyword or (keywords[0] if keywords else ""),
        canonical_url=payload.canonical_url,
        cover_image_url=payload.cover_image_url,
        source={"kind": "manual", "user_id": user.id},
    )
    _commit_content(db, content)
    return _to_detail(content)


@router.patch("/{content_id}", response_model=ContentDetail)
def update_content(
    content_id: int,
    payload: ContentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    content = _owned_content(content_id, db, user)
    data = payload.model_dump(exclude_unset=True)

    if content.status == ContentStatus.PUBLISHED and set(data) - {"status", "scheduled_for"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is already published. Editing it here would not "
            "change what is live on the platforms.",
        )

    if "title" in data and data["title"] != content.title:
        content.slug = unique_content_slug(db, content.project_id, data["title"])
    if "keywords" in data and data["keywords"] is not None:
        data["keywords"] = seo.normalize_keywords(data["keywords"])
        # Keep focus_keyword in sync unless the caller set it explicitly.
        if "focus_keyword" not in data and data["keywords"]:
            data["focus_keyword"] = data["keywords"][0]

    for key, value in data.items():
        setattr(content, key, value)

    db.commit()
    db.refresh(content)
    return _to_detail(content)


@router.delete("/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    content = _owned_content(content_id, db, user)
    db.delete(content)
    db.commit()


# --------------------------------------------------------------------------- #
# Workflow                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/{content_id}/approve", response_model=ContentOut)
def approve_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentOut:
    """Mark a piece approved, and release anything already queued for it.

    Approving is what un-blocks publication: a piece queued while still in
    review sits as ``pending`` until this happens, which is what makes the
    review queue meaningful rather than advisory.
    """
    content = _owned_content(content_id, db, user)
    if content.status == ContentStatus.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already published"
        )
    content.status = ContentStatus.APPROVED
    db.commit()
    db.refresh(content)
    return _to_out(content)


@router.post("/{content_id}/publish", response_model=list[PublicationOut])
def publish_content(
    content_id: int,
    payload: PublishRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Queue a piece for one or more platforms.

    Queuing is synchronous and cheap; the publishing itself is a worker's job
    (or runs inline when ``CELERY_ENABLED`` is off). The response is the queue
    state, not the outcome — the caller polls the publications for that.
    """
    content = _owned_content(content_id, db, user)

    unimplemented = [
        p.value for p in payload.platforms if not publishers.get_adapter(p).implemented
    ]
    if unimplemented:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No finished adapter for: {', '.join(unimplemented)}. "
                f"Publishing works for: "
                f"{', '.join(p.value for p in publishers.implemented_platforms())}."
            ),
        )

    missing = [
        p.value for p in payload.platforms if p.value not in user.connected_platforms
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not connected to: {', '.join(missing)}. Add credentials in Settings.",
        )

    # Last cheap chance to catch a URL the model invented. Only a definitive 404
    # or 410 stops the publish — see app.services.link_check on why a timeout
    # must not.
    if settings.link_check_enabled and not payload.allow_broken_links:
        dead = link_check.broken(_check_content_links(content))
        if dead:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{len(dead)} link(s) in this post are dead: "
                    + "; ".join(f"{s.url} ({s.http_status or 'unreachable'})" for s in dead)
                    + ". Fix them, or publish anyway with allow_broken_links."
                ),
            )

    publications = publishing_service.queue(
        db,
        content,
        list(payload.platforms),
        scheduled_for=payload.scheduled_for,
        as_draft=payload.as_draft,
    )

    if content.status in (ContentStatus.DRAFT, ContentStatus.REVIEW):
        content.status = ContentStatus.APPROVED
    content.scheduled_for = payload.scheduled_for
    db.commit()

    # Nothing scheduled goes out now. Scheduled work waits for the beat task.
    if payload.scheduled_for is None:
        _dispatch([p.id for p in publications])

    for publication in publications:
        db.refresh(publication)
    return [PublicationOut.model_validate(p) for p in publications]


def _dispatch(publication_ids: list[int]) -> None:
    """Hand publications to a worker, or run them inline.

    The inline path exists for single-process deployments and tests. It is also
    the fallback when the broker is unreachable: losing a publish because Redis
    is down would be a silent failure of the thing the user just clicked.
    """
    from app.tasks import publish_tasks

    if settings.celery_enabled:
        try:
            for publication_id in publication_ids:
                publish_tasks.publish_one.delay(publication_id)
            return
        except Exception as exc:  # pragma: no cover - broker down
            logger.warning("celery dispatch failed, publishing inline: %s", exc)

    for publication_id in publication_ids:
        publish_tasks.publish_one(publication_id)


@router.post("/{content_id}/retry/{publication_id}", response_model=PublicationOut)
def retry_publication(
    content_id: int,
    publication_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PublicationOut:
    """Re-arm one failed publication and try again."""
    content = _owned_content(content_id, db, user)
    publication = db.get(Publication, publication_id)
    if publication is None or publication.content_id != content.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found"
        )
    if publication.status == PublicationStatus.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already published — retrying would post it twice.",
        )

    publication.status = PublicationStatus.PENDING
    publication.attempts = 0
    publication.error = None
    publication.scheduled_for = None
    db.commit()

    _dispatch([publication.id])
    db.refresh(publication)
    return PublicationOut.model_validate(publication)


@router.post("/ideas/{idea_id}/write", response_model=ContentDetail, status_code=201)
def write_from_idea(
    idea_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Turn a suggested idea into a draft, and retire the idea."""
    idea = db.get(ContentIdea, idea_id)
    if idea is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Idea not found")
    project = owned_project(idea.project_id, db, user)

    generated = content_generator.generate(
        project,
        idea.content_type,
        instructions=f"Work to this angle: {idea.headline}. {idea.rationale}",
    )
    content = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=idea.content_type,
        generated=generated,
        status=ContentStatus.DRAFT,
        source={"kind": "idea", "idea_id": idea.id, "headline": idea.headline},
    )
    db.add(content)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        content.slug = f"{content.slug}-{secrets.token_hex(3)}"
        db.add(content)
        db.flush()
    idea.used_content_id = content.id
    db.commit()
    db.refresh(content)
    return _to_detail(content)
