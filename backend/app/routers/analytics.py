"""Analytics dashboard endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.content import Content, ContentStatus
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.analytics import (
    AlertsOut,
    DashboardOut,
    DigestOut,
    DigestSendOut,
    EngagementTrendPointOut,
    OverviewOut,
    ReadTimeOut,
    VelocityCurveDetailOut,
    VelocitySummaryOut,
)
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.services import alerts, analytics_service, digest, mailer, velocity

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/overview",
    response_model=OverviewOut,
    summary="Full analytics breakdown",
    responses=errors(*AUTHENTICATED),
)
def overview(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Everything the analytics page needs, in one round trip."""
    return analytics_service.overview(db, user.id)


@router.get(
    "/engagement-trend",
    response_model=list[EngagementTrendPointOut],
    summary="Views and engagement per day",
    responses=errors(*AUTHENTICATED),
)
def engagement_trend(
    days: int = Query(default=30, ge=1, le=180),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Views and engagement recorded per day, for the dashboard trend chart."""
    return analytics_service.engagement_trend(db, user.id, days=days)


@router.get(
    "/read-time",
    response_model=ReadTimeOut,
    summary="Post length against performance",
    responses=errors(*AUTHENTICATED),
)
def read_time(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """How long the published pieces are, and whether length pays off."""
    return analytics_service.read_time(db, user.id)


@router.get(
    "/velocity",
    response_model=VelocitySummaryOut,
    summary="How fast posts found an audience",
    responses=errors(*AUTHENTICATED),
)
def velocity_summary(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """How fast posts found an audience, and which have stopped growing.

    Read from the stored snapshot series rather than the latest row per
    publication — see :mod:`app.services.velocity`.
    """
    return velocity.summary(db, user.id)


@router.get(
    "/velocity/{publication_id}",
    response_model=VelocityCurveDetailOut,
    summary="One publication's growth curve",
    responses=errors(*OWNED),
)
def velocity_curve(
    publication_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """One publication's full growth curve, for the detail chart.

    The id is filtered in the query, not after it. Building every curve on the
    account to keep one made opening a single chart cost the entire snapshot
    history of every post the account had published — the same unbounded read
    :mod:`app.services.velocity` bounds for the calendar, on a page reached by
    clicking a row.
    """
    found = velocity.curves(db, user.id, publication_id=publication_id)
    if not found:
        # Also the answer for a publication belonging to someone else: the
        # ownership filter is in the query, so "not yours" and "not there" are
        # indistinguishable from here, which is the intended behaviour.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found"
        )
    curve = found[0]
    return {
        **curve.as_dict(),
        "points": [
            {"hours": round(p.hours, 2), "views": p.views, "engagement": p.engagement}
            for p in curve.points
        ],
    }


@router.get(
    "/alerts",
    response_model=AlertsOut,
    summary="Posts underperforming your own normal",
    responses=errors(*AUTHENTICATED),
)
def performance_alerts(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Posts doing measurably worse than this user's own normal."""
    return alerts.summary(db, user.id, limit=limit)


@router.get(
    "/digest",
    response_model=DigestOut,
    summary="This week's digest, as data",
    responses=errors(*AUTHENTICATED),
)
def digest_preview(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """This week's digest, as data. Sends nothing.

    The same object the Monday email is rendered from, so what the UI shows and
    what lands in the inbox cannot drift.
    """
    return digest.build(db, user).as_dict()


@router.post(
    "/digest/send",
    response_model=DigestSendOut,
    summary="Mail this week's digest now",
    responses=errors(*AUTHENTICATED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_digest_send, key_func=account_key)
def digest_send(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Mail this week's digest now.

    Rate-limited per account: the mail goes out through the install's single
    SMTP identity, so the budget being spent — and the sending reputation
    behind it — belongs to everyone here rather than to the caller.

    ``sent: false`` is a normal answer, not a failure — an empty week is not
    mailed, and neither is anything when SMTP is unconfigured. ``reason`` says
    which it was, so the UI need not guess.
    """
    built = digest.build(db, user)
    if built.is_empty:
        return {"sent": False, "reason": "Nothing happened this week."}
    if not mailer.configured():
        return {
            "sent": False,
            "reason": "SMTP is not configured — set SMTP_HOST to send mail.",
        }
    sent = digest.send(db, user)
    return {
        "sent": sent,
        "reason": "" if sent else "The mail server refused the message.",
        "subject": built.subject,
    }


@router.get(
    "/dashboard",
    response_model=DashboardOut,
    summary="Home-page counters and what needs attention",
    responses=errors(*AUTHENTICATED),
)
def dashboard(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """The home page: headline counters, what needs attention, what just happened.

    Separate from ``/overview`` because the dashboard is loaded far more often
    and does not need the full per-type/per-platform breakdown.
    """
    summary = analytics_service.totals(db, user.id)

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
    # One column off the piece — its title. ``joinedload(Publication.content)``
    # brought back the whole entity to read it: five article bodies and four
    # JSON columns each, next to the eight below, on the page every session
    # opens first. The join is already visiting ``content`` for the filter, so
    # naming the column costs nothing and loads no relationship to suppress.
    scheduled = db.execute(
        select(
            Publication.id,
            Publication.content_id,
            Content.title,
            Publication.platform,
            Publication.scheduled_for,
        )
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Publication.status == PublicationStatus.SCHEDULED,
        )
        .order_by(Publication.scheduled_for)
        .limit(5)
    ).all()
    # Seven columns, not entities. These eight rows render as an id, a title, a
    # status, a type, a project name and a date; a ``Content`` row to reach them
    # is the whole body and four JSON columns, and ``Content.publications`` is
    # ``lazy="selectin"`` for the content list's sake, so the default also fired
    # a second SELECT over every publication attached to them.
    recent = db.execute(
        select(
            Content.id,
            Content.title,
            Content.status,
            Content.content_type,
            Content.project_id,
            Project.name.label("project_name"),
            Content.created_at,
        )
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id)
        .order_by(Content.created_at.desc())
        .limit(8)
    ).all()

    return {
        "totals": summary.to_dict(),
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
                "title": p.title,
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
                "project_name": c.project_name,
                "created_at": c.created_at,
            }
            for c in recent
        ],
        "by_project": analytics_service.by_project(db, user.id),
        "timeline": analytics_service.timeline(db, user.id, days=14),
        # Capped tighter than the /alerts endpoint: this is the home page's
        # "what needs attention" column, not the full list.
        "alerts": [a.as_dict() for a in alerts.build(db, user.id, limit=5)],
    }
