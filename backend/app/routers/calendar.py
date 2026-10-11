"""Content calendar: what is going out, when, and where there is room for more."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, refresh_all
from app.deps import QueryRowId, RowId, get_current_user, owned_project
from app.models.content import Content, ContentStatus
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import (
    CadenceGuideOut,
    CalendarEntry,
    CalendarOut,
    PublicationOut,
    ScheduleUpdate,
)
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.services import cadence, learned_cadence, publishing_service, scheduling, velocity

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get(
    "",
    response_model=CalendarOut,
    summary="Scheduled and published items in a date window",
    responses=errors(status.HTTP_400_BAD_REQUEST, *OWNED),
)
def get_calendar(
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: QueryRowId | None = None,
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
    # Columns, not entities. A calendar row needs ten fields; selecting the
    # three entities to reach them brought every article body in the window
    # along with them, and ``Content.publications`` is ``lazy="selectin"``, so
    # it also fired a second query for every publication of every piece on the
    # calendar — including the ones outside the window the WHERE above was
    # written to bound. Labelled because three of the ten are called ``id``.
    query = (
        select(
            Publication.id.label("publication_id"),
            Publication.platform,
            Publication.status.label("publication_status"),
            Publication.published_at,
            Publication.scheduled_for,
            Content.id.label("content_id"),
            Content.title,
            Content.content_type,
            Project.id.label("project_id"),
            Project.name.label("project_name"),
        )
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
        owned_project(project_id, db, user)
        query = query.where(Content.project_id == project_id)

    entries: list[CalendarEntry] = []
    for row in db.execute(query).all():
        # Never None: the WHERE above admits a row only if one of these two
        # columns falls inside the window, and NULL never satisfies BETWEEN.
        # A publication queued with no time at all is therefore not on the
        # calendar at all — see
        # test_a_publication_with_no_time_at_all_is_not_on_the_calendar.
        when = row.published_at or row.scheduled_for
        entries.append(
            CalendarEntry(
                content_id=row.content_id,
                publication_id=row.publication_id,
                title=row.title,
                project_id=row.project_id,
                project_name=row.project_name,
                content_type=row.content_type,
                platform=row.platform,
                status=row.publication_status.value,
                when=when,
                movable=row.publication_status
                not in (PublicationStatus.PUBLISHED, PublicationStatus.PUBLISHING),
            )
        )

    # Content scheduled but not yet assigned to any platform still belongs on
    # the calendar — otherwise "schedule this for Tuesday" makes it disappear
    # until it is also routed somewhere.
    unrouted = db.execute(
        # Columns for the same reason as above. ``~Content.publications.any()``
        # is an EXISTS subquery, which is not the same thing as the eager load
        # the entity would still have fired to fetch the rows it just proved
        # were absent.
        select(
            Content.id.label("content_id"),
            Content.title,
            Content.content_type,
            Content.status,
            Content.scheduled_for,
            Project.id.label("project_id"),
            Project.name.label("project_name"),
        )
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Content.scheduled_for.is_not(None),
            # PUBLISHED is excluded because a piece that went out is drawn from
            # its publications, above, not from the date it was aiming for.
            # ARCHIVED is excluded because it is not going out at all: it was
            # still being drawn as an upcoming, ``movable=True`` entry, so the
            # calendar showed a rejected piece as this coming Tuesday's post and
            # the cadence suggester below counted its slot as ``taken`` and
            # steered the user's next real post away from it. Archiving now
            # clears the column too (``publishing_service.cancel_armed``), so
            # this is what covers the rows archived before that shipped.
            Content.status.not_in(
                (ContentStatus.PUBLISHED, ContentStatus.ARCHIVED)
            ),
            ~Content.publications.any(),
        )
    ).all()
    for row in unrouted:
        if not (window_start <= as_aware(row.scheduled_for) <= window_end):
            continue
        entries.append(
            CalendarEntry(
                content_id=row.content_id,
                publication_id=None,
                title=row.title,
                project_id=row.project_id,
                project_name=row.project_name,
                content_type=row.content_type,
                platform=None,
                status=row.status.value,
                when=row.scheduled_for,
                movable=True,
            )
        )

    entries.sort(key=lambda e: e.when)

    connected = [Platform(p) for p in user.connected_platforms]
    taken = [as_aware(e.when) for e in entries if as_aware(e.when) >= utcnow()]
    # One pass over the metric series for every connected platform, rather than
    # one per platform inside the loop — and bounded to the window the learned
    # cadence actually reads, so the page does not get slower every month the
    # account stays open. Everything below asks these curves for first-window
    # views and nothing else; ``within_hours`` makes that a promise the curve
    # enforces rather than a convention.
    known = (
        velocity.curves(
            db, user.id, within_hours=float(settings.velocity_early_window_hours)
        )
        if settings.learned_cadence_enabled
        else []
    )
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
        cadence=[
            CadenceGuideOut(**entry)
            for entry in learned_cadence.describe_all(db, user.id, list(connected), known=known)
        ],
        suggested_slots=sorted(set(suggested))[:6],
    )


@router.patch(
    "/content/{content_id}",
    response_model=list[PublicationOut],
    summary="Move a scheduled item to another time",
    responses=errors(
        *OWNED,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
def reschedule(
    content_id: RowId,
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

    ``timezone`` reads a ``scheduled_for`` with no offset on it as a wall-clock
    time in that zone. A calendar that draws local days is exactly where this
    matters: dropping a card on "Tuesday 09:00" three months out means 09:00 in
    the user's own week, and an offset the browser resolved today is the wrong
    one if a daylight-saving change falls in between.
    """
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found."
        )

    # Rescheduling is arming: the loop below puts every target back to
    # ``SCHEDULED`` with its attempts reset, cancelled rows included. On an
    # archived piece that would undo the cancellation archiving just performed,
    # and the piece would sit armed until a worker reached
    # ``publishing_service.execute`` and cancelled it again. Nothing on the
    # calendar drags here any more — an archived piece is no longer drawn — so
    # this is the API surface, and the answer is the same one the publish
    # endpoints give.
    if content.status == ContentStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is archived, which means it is not going out. "
            "Take it out of the archive first.",
        )

    try:
        when = scheduling.normalize(payload.scheduled_for, tz=payload.timezone)
    except scheduling.ScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    targets = [
        p
        for p in content.publications
        if payload.publication_id is None or p.id == payload.publication_id
    ]
    if payload.publication_id is not None and not targets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found."
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

    # Dragging a piece onto a date arms it, and arming a draft approves it —
    # see :func:`app.services.publishing_service.arming_approves`. Without this
    # a draft whose demotion had just cancelled its rows could be dragged back
    # onto the calendar and go out while the column still said ``draft``.
    if targets:
        publishing_service.arming_approves(content)
        publishing_service.sync_content_status(content)

    db.commit()
    refresh_all(db, targets)
    return [PublicationOut.model_validate(p) for p in targets]


@router.get(
    "/cadence",
    response_model=list[CadenceGuideOut],
    summary="Suggested posting rhythm per platform",
    responses=errors(*AUTHENTICATED),
)
def cadence_guide(
    platform: Platform | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[CadenceGuideOut]:
    """Suggested posting rhythm — for the connected platforms, or one named one.

    Each entry carries ``source``: ``learned`` when the hours came from this
    user's own results, ``table`` when there is not enough evidence yet and the
    generic guidance stands. The UI shows which, because a suggestion the user
    cannot interrogate is one they are right to ignore.
    """
    targets = [platform] if platform is not None else (
        user.connected_platforms or [p.value for p in Platform]
    )
    return [
        CadenceGuideOut(**entry)
        for entry in learned_cadence.describe_all(db, user.id, list(targets))
    ]
