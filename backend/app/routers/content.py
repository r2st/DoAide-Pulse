"""Content: generate, edit, review, approve, publish."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, owned_project
from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.project import Project, slugify
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import (
    ContentCreate,
    ContentDetail,
    ContentOut,
    ContentUpdate,
    GenerateRequest,
    PublicationOut,
    PublishRequestIn,
    SeoIssueOut,
)
from app.services import (
    content_generator,
    github_client,
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
    )
    return ContentDetail(
        **_to_out(content).model_dump(),
        body_markdown=content.body_markdown,
        seo_issues=[
            SeoIssueOut(level=i.level, field=i.field, message=i.message) for i in issues
        ],
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


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ContentOut])
def list_content(
    project_id: int | None = None,
    status_filter: ContentStatus | None = Query(default=None, alias="status"),
    content_type: ContentType | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ContentOut]:
    query = (
        select(Content)
        .join(Project, Project.id == Content.project_id)
        # ``_to_out`` reads ``content.project.name`` for every row. The join
        # above only filters — it does not populate the relationship — so
        # without this the default lazy load fires one SELECT per row, and this
        # endpoint returns up to 500 of them.
        .options(joinedload(Content.project))
        .where(Project.user_id == user.id)
        .order_by(Content.created_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        query = query.where(Content.project_id == project_id)
    if status_filter is not None:
        query = query.where(Content.status == status_filter)
    if content_type is not None:
        query = query.where(Content.content_type == content_type)

    return [_to_out(c) for c in db.scalars(query)]


# NB: the literal paths under /content (``/queue/...``) are declared here,
# *before* ``GET /{content_id}``. FastAPI matches in declaration order, so a
# ``/{content_id}`` registered first would swallow ``/queue/review`` and 422 on
# "queue" not being an int.


@router.get("/queue/review", response_model=list[ContentOut])
def review_queue(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ContentOut]:
    """Everything the autopilot wrote that is waiting on a human."""
    rows = db.scalars(
        select(Content)
        .join(Project, Project.id == Content.project_id)
        .options(joinedload(Content.project))
        .where(Project.user_id == user.id, Content.status == ContentStatus.REVIEW)
        .order_by(Content.created_at.desc())
    )
    return [_to_out(c) for c in rows]


@router.get("/queue/publications", response_model=list[PublicationOut])
def publication_queue(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[PublicationOut]:
    """Everything in flight or waiting: pending, scheduled, publishing, failed."""
    rows = db.scalars(
        select(Publication)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Publication.status != PublicationStatus.PUBLISHED,
        )
        .order_by(Publication.scheduled_for.is_(None).desc(), Publication.scheduled_for)
    )
    return [PublicationOut.model_validate(p) for p in rows]


@router.get("/{content_id}", response_model=ContentDetail)
def get_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    return _to_detail(_owned_content(content_id, db, user))


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

    content = Content(
        project_id=project.id,
        content_type=payload.content_type,
        status=ContentStatus.DRAFT,
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
            "kind": "manual",
            "user_id": user.id,
            "instructions": payload.instructions,
            "fallback": generated.is_fallback,
        },
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return _to_detail(content)


@router.post("", response_model=ContentDetail, status_code=status.HTTP_201_CREATED)
def create_content(
    payload: ContentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Write a piece by hand."""
    project = owned_project(payload.project_id, db, user)
    content = Content(
        project_id=project.id,
        content_type=payload.content_type,
        status=ContentStatus.DRAFT,
        title=payload.title,
        slug=_unique_slug(db, project.id, payload.title),
        body_markdown=payload.body_markdown,
        excerpt=payload.excerpt or seo.build_excerpt(payload.body_markdown),
        meta_description=payload.meta_description
        or seo.build_meta_description("", fallback_body=payload.body_markdown),
        keywords=seo.normalize_keywords(payload.keywords),
        tags=payload.tags,
        canonical_url=payload.canonical_url,
        source={"kind": "manual", "user_id": user.id},
    )
    db.add(content)
    db.commit()
    db.refresh(content)
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
        content.slug = _unique_slug(db, content.project_id, data["title"])
    if "keywords" in data and data["keywords"] is not None:
        data["keywords"] = seo.normalize_keywords(data["keywords"])

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

    publications = publishing_service.queue(
        db, content, list(payload.platforms), scheduled_for=payload.scheduled_for
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
    content = Content(
        project_id=project.id,
        content_type=idea.content_type,
        status=ContentStatus.DRAFT,
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
        source={"kind": "idea", "idea_id": idea.id, "headline": idea.headline},
    )
    db.add(content)
    db.flush()
    idea.used_content_id = content.id
    db.commit()
    db.refresh(content)
    return _to_detail(content)
