"""Analytics dashboard endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user
from app.models.content import Content, ContentStatus
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview")
def overview(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Everything the analytics page needs, in one round trip."""
    return analytics_service.overview(db, user.id)


@router.get("/dashboard")
def dashboard(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """The home page: headline counters, what needs attention, what just happened.

    Separate from ``/overview`` because the dashboard is loaded far more often
    and does not need the full per-type/per-platform breakdown.
    """
    totals = analytics_service.totals(db, user.id)

    review_count = db.scalar(
        select(func.count(Content.id))
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id, Content.status == ContentStatus.REVIEW)
    ) or 0
    failed = list(
        db.scalars(
            select(Publication)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(
                Project.user_id == user.id,
                Publication.status == PublicationStatus.FAILED,
            )
            .order_by(Publication.updated_at.desc())
            .limit(5)
        )
    )
    scheduled = list(
        db.scalars(
            select(Publication)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            # The response reads ``p.content.title``; joining for the filter
            # does not load the relationship.
            .options(joinedload(Publication.content))
            .where(
                Project.user_id == user.id,
                Publication.status == PublicationStatus.SCHEDULED,
            )
            .order_by(Publication.scheduled_for)
            .limit(5)
        )
    )
    recent = list(
        db.scalars(
            select(Content)
            .join(Project, Project.id == Content.project_id)
            # Same as the content list: ``c.project.name`` per row.
            .options(joinedload(Content.project))
            .where(Project.user_id == user.id)
            .order_by(Content.created_at.desc())
            .limit(8)
        )
    )

    return {
        "totals": totals.__dict__,
        "needs_review": review_count,
        "failed_publications": [
            {
                "id": p.id,
                "content_id": p.content_id,
                "platform": p.platform.value,
                "error": p.error,
            }
            for p in failed
        ],
        "upcoming": [
            {
                "id": p.id,
                "content_id": p.content_id,
                "title": p.content.title,
                "platform": p.platform.value,
                "scheduled_for": p.scheduled_for,
            }
            for p in scheduled
        ],
        "recent_content": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status.value,
                "content_type": c.content_type.value,
                "project_id": c.project_id,
                "project_name": c.project.name,
                "created_at": c.created_at,
            }
            for c in recent
        ],
        "by_project": analytics_service.by_project(db, user.id),
        "timeline": analytics_service.timeline(db, user.id, days=14),
    }
