"""Project registry: register what Herald should write about."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, owned_project
from app.models.content import Content, ContentIdea, ContentStatus
from app.models.mixins import utcnow
from app.models.project import Project, slugify
from app.models.user import User
from app.schemas.project import (
    IdeaOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RepoActivityOut,
)
from app.services import content_generator, github_client

router = APIRouter(prefix="/projects", tags=["projects"])


def _unique_slug(db: Session, user_id: int, name: str, *, exclude_id: int | None = None) -> str:
    """A slug unique within this user's projects.

    Scoped per user rather than globally: two people registering a project
    called "Herald" should both get ``herald``.
    """
    base = slugify(name)
    candidate = base
    suffix = 2
    while True:
        query = select(Project.id).where(
            Project.user_id == user_id, Project.slug == candidate
        )
        if exclude_id is not None:
            query = query.where(Project.id != exclude_id)
        if db.scalar(query) is None:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _project_fields(project: Project) -> dict:
    """The column values shared by every serialisation path."""
    return {
        key: getattr(project, key)
        for key in (
            "id", "name", "slug", "description", "repo_url", "live_url",
            "tech_stack", "target_audience", "keywords", "tone", "is_active",
            "autopilot_mode", "autopilot_platforms", "auto_canonical",
            "canonical_platform", "utm_enabled", "utm_campaign",
            "last_seen_commit_sha",
            "last_seen_release_tag", "last_scanned_at", "created_at",
        )
    }


def _to_out(
    project: Project,
    *,
    content_count: int = 0,
    published_count: int = 0,
    db: Session | None = None,
) -> ProjectOut:
    """Serialize a project with content counters.

    When ``db`` is passed the counts are looked up on the spot (single-project
    views). When the caller already has them — the list endpoint batches the
    query — they are passed directly and no extra SQL is emitted.
    """
    if db is not None:
        counts = db.execute(
            select(Content.status, func.count(Content.id))
            .where(Content.project_id == project.id)
            .group_by(Content.status)
        ).all()
        content_count = sum(count for _, count in counts)
        published_count = next(
            (count for status_, count in counts if status_ == ContentStatus.PUBLISHED), 0
        )
    return ProjectOut(
        **{
            **_project_fields(project),
            "repo_full_name": project.repo_full_name,
            "content_count": content_count,
            "published_count": published_count,
        }
    )


def _batch_counts(db: Session, project_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Fetch (total, published) content counts for a batch of projects in one query."""
    if not project_ids:
        return {}
    rows = db.execute(
        select(
            Content.project_id,
            func.count(Content.id),
            func.count(Content.id).filter(Content.status == ContentStatus.PUBLISHED),
        )
        .where(Content.project_id.in_(project_ids))
        .group_by(Content.project_id)
    ).all()
    return {pid: (total, published) for pid, total, published in rows}


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ProjectOut]:
    projects = list(
        db.scalars(
            select(Project).where(Project.user_id == user.id).order_by(Project.name)
        )
    )
    counts = _batch_counts(db, [p.id for p in projects])
    return [
        _to_out(p, content_count=counts.get(p.id, (0, 0))[0],
                published_count=counts.get(p.id, (0, 0))[1])
        for p in projects
    ]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    base_slug = _unique_slug(db, user.id, payload.name)
    project = Project(
        user_id=user.id,
        slug=base_slug,
        **payload.model_dump(exclude={"autopilot_platforms"}),
        autopilot_platforms=[p.value for p in payload.autopilot_platforms],
    )
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # The original instance is expunged after rollback — build a fresh
        # one rather than re-adding a detached object with stale state.
        project = Project(
            user_id=user.id,
            slug=f"{base_slug}-{secrets.token_hex(3)}",
            **payload.model_dump(exclude={"autopilot_platforms"}),
            autopilot_platforms=[p.value for p in payload.autopilot_platforms],
        )
        db.add(project)
        db.commit()
    db.refresh(project)
    return _to_out(project, db=db)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    return _to_out(owned_project(project_id, db, user), db=db)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    project = owned_project(project_id, db, user)
    data = payload.model_dump(exclude_unset=True)

    if "autopilot_platforms" in data and data["autopilot_platforms"] is not None:
        data["autopilot_platforms"] = [
            p.value if hasattr(p, "value") else str(p) for p in data["autopilot_platforms"]
        ]
    # Renaming re-slugs, but only if the name actually changed — otherwise a
    # PATCH that touches nothing would bump `herald` to `herald-2`.
    if "name" in data and data["name"] != project.name:
        project.slug = _unique_slug(db, user.id, data["name"], exclude_id=project.id)

    for key, value in data.items():
        setattr(project, key, value)

    db.commit()
    db.refresh(project)
    return _to_out(project, db=db)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    project = owned_project(project_id, db, user)
    db.delete(project)
    db.commit()


@router.post("/{project_id}/scan", response_model=RepoActivityOut)
def scan_repo(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RepoActivityOut:
    """Fetch what has shipped since the last scan, and move the watermark.

    Moving the watermark here is deliberate: a manual scan is the user saying
    "I've seen this", so the autopilot should not then write about the same
    commits an hour later.
    """
    project = owned_project(project_id, db, user)
    full_name = project.repo_full_name
    if not full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This project has no GitHub repo URL to scan.",
        )

    try:
        activity = github_client.fetch_activity(
            full_name,
            since_sha=project.last_seen_commit_sha,
            since_tag=project.last_seen_release_tag,
        )
    except github_client.GitHubRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except github_client.GitHubError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    project.last_seen_commit_sha = activity.head_sha
    project.last_seen_release_tag = activity.latest_tag
    project.last_scanned_at = utcnow()
    db.commit()

    return RepoActivityOut(
        full_name=activity.full_name,
        new_commit_count=len(activity.new_commits),
        new_release_tag=activity.new_release.tag if activity.new_release else None,
        stars=activity.stars,
        description=activity.description,
        topics=activity.topics,
        commits=[c.summary for c in activity.new_commits[:20]],
    )


@router.get("/{project_id}/ideas", response_model=list[IdeaOut])
def list_ideas(
    project_id: int,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[IdeaOut]:
    """Subjects worth writing about. ``refresh=true`` asks the model for more.

    Without ``refresh`` this is a cheap read of what the autopilot has already
    banked, so the projects page can show ideas without an LLM call per visit.
    """
    project = owned_project(project_id, db, user)

    if refresh:
        ideas = content_generator.suggest_ideas(project)
        for idea in ideas:
            db.add(
                ContentIdea(
                    project_id=project.id,
                    content_type=idea.content_type,
                    headline=idea.headline,
                    rationale=idea.rationale,
                    source={"kind": "manual_refresh"},
                )
            )
        db.commit()

    rows = db.scalars(
        select(ContentIdea)
        .where(ContentIdea.project_id == project.id, ContentIdea.used_content_id.is_(None))
        .order_by(ContentIdea.created_at.desc())
        .limit(12)
    )
    return [IdeaOut.model_validate(row) for row in rows]
