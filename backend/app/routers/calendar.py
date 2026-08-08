"""Content calendar: what is going out, when, and where there is room for more."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.content import Content, ContentStatus
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import CalendarEntry, CalendarOut, PublicationOut, ScheduleUpdate
from app.services import cadence, learned_cadence, scheduling, velocity

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
    window_start = as_aware(start) if start else (utcnow() - timedelta(days=30))
    window_end = as_aware(end) if end else (utcnow() + timedelta(days=30))

    # Cap the window so a careless or malicious request doesn't scan years of
    # publications into memory.
    max_window = timedelta(days=365)
    if (window_end - window_start) > max_window:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Calendar window cannot exceed 365 days.",
        )

    # Push the date filter into SQL so we never load the full publication table
    # into memory. A publication's calendar position is ``published_at`` if set,
    # else ``scheduled_for``, so both columns are checked against the window.
    query = (
        select(Publication, Content, Project)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            or_(
                Publication.published_at.between(window_start, window_end),
                Publication.scheduled_for.between(window_start, window_end),
            ),
        )
    )
    if project_id is not None:
        query = query.where(Content.project_id == project_id)

    entries: list[CalendarEntry] = []
    for publication, content, project in db.execute(query).all():
        when = publication.published_at or publication.scheduled_for
        if when is None:
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
        if not (window_start <= as_aware(content.scheduled_for) <= window_end):
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
    taken = [as_aware(e.when) for e in entries if as_aware(e.when) >= utcnow()]
    # One pass over the metric series for every connected platform, rather than
    # one per platform inside the loop.
    known = velocity.curves(db, user.id) if settings.learned_cadence_enabled else []
    suggested: list[datetime] = []
    for platform in connected:
        learned = learned_cadence.learn(db, user.id, platform, known=known)
        suggested.extend(
            slot
            for slot in cadence.suggest_schedule(
                platform, count=2, start=utcnow(), using=learned.cadence
            )
            if all(abs((slot - as_aware(t)).total_seconds()) > 12 * 3600 for t in taken)
        )

    return CalendarOut(
        entries=entries,
        # ``known=`` for the same reason it is passed to ``learn`` above: these
        # are the curves built at the top of this function, and letting
        # ``describe_all`` rebuild them read the entire metric series a second
        # time on every calendar load.
        cadence=learned_cadence.describe_all(db, user.id, list(connected), known=known),
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

    The requested time goes through :func:`app.services.scheduling.normalize`,
    exactly as it does on the publish and schedule endpoints. Drag-and-drop
    reaches this route rather than those, and without the same guard the one
    validation that matters most is missing from the one surface where a
    mis-drop is easiest: a slot behind "now" is not a schedule at all, it is a
    publish on the next sweep wearing a date.
    """
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found"
        )

    try:
        when = scheduling.normalize(payload.scheduled_for)
    except scheduling.ScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    targets = [
        p
        for p in content.publications
        if payload.publication_id is None or p.id == payload.publication_id
    ]
    if payload.publication_id is not None and not targets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found"
        )

    # PUBLISHING as well as PUBLISHED: a row a worker has already claimed is
    # mid-flight, and moving it back to SCHEDULED re-arms a publication that is
    # about to succeed — the next sweep then posts it a second time. This is
    # the same pair the calendar itself reports as ``movable=False``, so an
    # attempt to move one is a client racing the worker rather than a user
    # doing something reasonable.
    settled = {PublicationStatus.PUBLISHED, PublicationStatus.PUBLISHING}
    in_flight = [p for p in targets if p.status in settled]
    if in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot reschedule something already published or going out "
            "now on " + ", ".join(p.platform.value for p in in_flight),
        )

    for publication in targets:
        publication.scheduled_for = when
        publication.status = (
            PublicationStatus.SCHEDULED if when else PublicationStatus.PENDING
        )
        # A move is a fresh start: a row that had burned two retries should not
        # arrive at its new slot with one left.
        publication.attempts = 0
        publication.error = None

    if payload.publication_id is None:
        content.scheduled_for = when

    db.commit()
    for publication in targets:
        db.refresh(publication)
    return [PublicationOut.model_validate(p) for p in targets]


@router.get("/cadence", response_model=list[dict])
def cadence_guide(
    platform: Platform | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Suggested posting rhythm — for the connected platforms, or one named one.

    Each entry carries ``source``: ``learned`` when the hours came from this
    user's own results, ``table`` when there is not enough evidence yet and the
    generic guidance stands. The UI shows which, because a suggestion the user
    cannot interrogate is one they are right to ignore.
    """
    targets = [platform] if platform is not None else (
        user.connected_platforms or [p.value for p in Platform]
    )
    return learned_cadence.describe_all(db, user.id, list(targets))
