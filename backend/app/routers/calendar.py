"""Content calendar: what is going out, when, and where there is room for more."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.content import Content, ContentStatus
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import CalendarEntry, CalendarOut, PublicationOut, ScheduleUpdate
from app.services import cadence

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("", response_model=CalendarOut)
def get_calendar(
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CalendarOut:
    """Everything on the calendar in a window, plus cadence guidance.

    Default window is a month back and a month forward: enough history to see
    the rhythm, enough future to plan into. Published items are included but
    flagged ``movable=False`` — you cannot reschedule the past.
    """
    window_start = start or (utcnow() - timedelta(days=30))
    window_end = end or (utcnow() + timedelta(days=30))

    query = (
        select(Publication, Content, Project)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id)
    )
    if project_id is not None:
        query = query.where(Content.project_id == project_id)

    entries: list[CalendarEntry] = []
    for publication, content, project in db.execute(query).all():
        when = publication.published_at or publication.scheduled_for
        if when is None or not (window_start <= when <= window_end):
            continue
        entries.append(
            CalendarEntry(
                content_id=content.id,
                publication_id=publication.id,
                title=content.title,
                project_id=project.id,
                project_name=project.name,
                content_type=content.content_type,
                platform=publication.platform,
                status=publication.status.value,
                when=when,
                movable=publication.status
                not in (PublicationStatus.PUBLISHED, PublicationStatus.PUBLISHING),
            )
        )

    # Content scheduled but not yet assigned to any platform still belongs on
    # the calendar — otherwise "schedule this for Tuesday" makes it disappear
    # until it is also routed somewhere.
    unrouted = db.execute(
        select(Content, Project)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Content.scheduled_for.is_not(None),
            Content.status != ContentStatus.PUBLISHED,
            ~Content.publications.any(),
        )
    ).all()
    for content, project in unrouted:
        if not (window_start <= content.scheduled_for <= window_end):
            continue
        entries.append(
            CalendarEntry(
                content_id=content.id,
                publication_id=None,
                title=content.title,
                project_id=project.id,
                project_name=project.name,
                content_type=content.content_type,
                platform=None,
                status=content.status.value,
                when=content.scheduled_for,
                movable=True,
            )
        )

    entries.sort(key=lambda e: e.when)

    connected = [Platform(p) for p in user.connected_platforms]
    taken = [e.when for e in entries if e.when >= utcnow()]
    suggested: list[datetime] = []
    for platform in connected:
        suggested.extend(
            slot
            for slot in cadence.suggest_schedule(platform, count=2, start=utcnow())
            if all(abs((slot - t).total_seconds()) > 12 * 3600 for t in taken)
        )

    return CalendarOut(
        entries=entries,
        cadence=[cadence.describe(p) for p in connected],
        suggested_slots=sorted(set(suggested))[:6],
    )


@router.patch("/content/{content_id}", response_model=list[PublicationOut])
def reschedule(
    content_id: int,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Move a calendar item. This is where drag-and-drop lands.

    Without ``publication_id`` the whole piece moves — every platform it is
    queued for. With one, only that platform moves, which is how you stagger a
    cross-post across a week.
    """
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found"
        )

    targets = [
        p
        for p in content.publications
        if payload.publication_id is None or p.id == payload.publication_id
    ]
    if payload.publication_id is not None and not targets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found"
        )

    already_live = [p for p in targets if p.status == PublicationStatus.PUBLISHED]
    if already_live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot reschedule something already published on "
            + ", ".join(p.platform.value for p in already_live),
        )

    for publication in targets:
        publication.scheduled_for = payload.scheduled_for
        publication.status = (
            PublicationStatus.SCHEDULED
            if payload.scheduled_for
            else PublicationStatus.PENDING
        )
        # A move is a fresh start: a row that had burned two retries should not
        # arrive at its new slot with one left.
        publication.attempts = 0
        publication.error = None

    if payload.publication_id is None:
        content.scheduled_for = payload.scheduled_for

    db.commit()
    for publication in targets:
        db.refresh(publication)
    return [PublicationOut.model_validate(p) for p in targets]


@router.get("/cadence", response_model=list[dict])
def cadence_guide(
    platform: Platform | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Suggested posting rhythm — for the connected platforms, or one named one."""
    if platform is not None:
        return [cadence.describe(platform)]
    connected = user.connected_platforms or [p.value for p in Platform]
    return [cadence.describe(p) for p in connected]
