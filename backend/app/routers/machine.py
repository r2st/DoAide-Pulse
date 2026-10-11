"""The surface an API key unlocks.

Separate from :mod:`app.routers.api_keys`, which is where keys are *managed*,
for two reasons. The first is that the two halves authenticate differently —
minting a credential is something a person does with a session token, using one
is something a build server does with the credential — and a module holding both
is a module where a route can pick up the wrong security scheme by sitting in
the wrong half of the file. The second is mechanical: Pulse's route sweeps
enumerate one ``router`` per module in this package, so a second router beside
the first would be a set of endpoints no sweep examines. Endpoints reachable
with a leaked credential are the last ones that should be invisible to the
tenant-isolation walk.

Every route here takes its project from the key rather than from the request. A
machine endpoint accepting a ``project_id`` would have to check it against the
credential's, and a check is something the next route added can forget; reading
it off the credential cannot be.

Nothing here can write a piece or publish one. The widest scope
(``content:write``) files an *idea*, which is inert until a human or the
autopilot acts on it — so the worst a leaked key does is put a suggestion in a
queue, and the worst a leaked read-only key does is enumerate what is already
public plus its own project's titles.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import ListOffset, require_scope
from app.models.api_key import ApiKey, ApiKeyScope
from app.models.content import Content, ContentIdea, ContentStatus
from app.models.metrics import ContentMetric
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.ratelimit import api_key_key, limiter
from app.schemas.api_key import (
    MachineAnalyticsOut,
    MachineContentOut,
    MachineIdeaCreate,
    MachineIdeaOut,
    MachineIdentityOut,
)
from app.schemas.errors import errors
from app.services import content_generator

router = APIRouter(prefix="/machine", tags=["machine"])

#: What every route here can return besides its success model. 401 for a bad
#: credential, 413 for an oversized body refused by middleware, 429 for the
#: limiter. 403 is added per-route, since only the scoped ones can produce it.
MACHINE_ERRORS = (
    status.HTTP_401_UNAUTHORIZED,
    status.HTTP_413_CONTENT_TOO_LARGE,
    status.HTTP_429_TOO_MANY_REQUESTS,
)

#: How many pieces one listing may return. Lower than the app's own listings:
#: a machine polls, and a poll that pages is a poll that notices when its page
#: is full.
MAX_MACHINE_PAGE = 100


@router.get(
    "/whoami",
    response_model=MachineIdentityOut,
    summary="What this key is",
    responses=errors(*MACHINE_ERRORS),
)
@limiter.limit(settings.rate_limit_machine_api, key_func=api_key_key)
def machine_whoami(
    request: Request,
    response: Response,
    key: ApiKey = Depends(require_scope()),
    db: Session = Depends(get_db),
) -> MachineIdentityOut:
    """Introspect the calling credential.

    The one route here that requires no scope, because a job's first question
    is "is my key alive, and does it carry what I think it carries?" — and
    answering that must not itself need a permission.

    ``request`` and ``response`` are slowapi's, not FastAPI's: the limiter reads
    its state off the first and writes ``X-RateLimit-*`` onto the second. An
    endpoint returning a model rather than a ``Response`` raises without the
    second one, on every call rather than only a limited one.
    """
    project = db.get(Project, key.project_id)
    return MachineIdentityOut(
        key_id=key.id,
        prefix=key.prefix,
        name=key.name,
        project_id=key.project_id,
        # The foreign key makes a missing project unreachable; the fallback is
        # here so a row deleted underneath a live request answers with a blank
        # slug rather than 500ing on ``None.slug``.
        project_slug=project.slug if project else "",
        scopes=list(key.scopes or []),
        expires_at=key.expires_at,
    )


@router.get(
    "/content",
    response_model=list[MachineContentOut],
    summary="The project's pieces",
    responses=errors(*MACHINE_ERRORS, status.HTTP_403_FORBIDDEN),
)
@limiter.limit(settings.rate_limit_machine_api, key_func=api_key_key)
def machine_content(
    request: Request,
    response: Response,
    status_filter: ContentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=MAX_MACHINE_PAGE),
    offset: ListOffset = 0,
    key: ApiKey = Depends(require_scope(ApiKeyScope.CONTENT_READ)),
    db: Session = Depends(get_db),
) -> list[MachineContentOut]:
    """The key's project's pieces, newest first.

    Only the columns a machine has a use for. The body is deliberately absent:
    it keeps this a narrow SELECT however long the articles get, and it means a
    leaked read-only key cannot pull out unpublished prose.
    """
    query = select(Content).where(Content.project_id == key.project_id)
    if status_filter is not None:
        query = query.where(Content.status == status_filter)

    total = db.scalar(
        select(func.count()).select_from(query.with_only_columns(Content.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)

    # Ordered by id after the timestamp so the page boundary is a total order:
    # two pieces created in the same second must not be able to swap places
    # between page one and page two, which would drop one from both. Same rule
    # as every other listing in this tree — see ``test_paging_is_a_total_order``.
    rows = db.scalars(
        query.order_by(Content.created_at.desc(), Content.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        MachineContentOut(
            id=row.id,
            title=row.title,
            slug=row.slug,
            content_type=row.content_type.value,
            status=row.status.value,
            word_count=row.word_count,
            canonical_url=row.canonical_url,
            published_at=row.published_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.post(
    "/ideas",
    response_model=MachineIdeaOut,
    status_code=status.HTTP_201_CREATED,
    summary="File an idea",
    responses={
        **errors(
            *MACHINE_ERRORS,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
        # Two success codes, both spelled out — the same shape as
        # ``POST /content/ideas/{id}/write``, and for the same reason: a reader
        # shown a described 200 beside a bare 201 is left to guess which is the
        # replay.
        status.HTTP_201_CREATED: {
            "model": MachineIdeaOut,
            "description": "The idea was filed and is waiting in the project's queue.",
        },
        status.HTTP_200_OK: {
            "model": MachineIdeaOut,
            "description": (
                "An open idea in this project already says this. It is "
                "returned instead of a duplicate — a job that retries, or "
                "fires once per pipeline run, sees the same id each time."
            ),
        },
    },
)
@limiter.limit(settings.rate_limit_machine_api, key_func=api_key_key)
def machine_idea(
    request: Request,
    response: Response,
    payload: MachineIdeaCreate,
    key: ApiKey = Depends(require_scope(ApiKeyScope.CONTENT_WRITE)),
    db: Session = Depends(get_db),
) -> MachineIdeaOut:
    """Put a subject in the project's queue for a human or the autopilot.

    The furthest a machine credential reaches into writing, and deliberately
    not very far: an idea is inert, shows up as a suggestion, and nothing
    publishes it. That is what makes the scope safe to hand a build server.

    ``source`` records that this arrived over the API and which key filed it, so
    a suggestion the autopilot later acts on can be traced back to its origin.

    Filed through the same door the autopilot uses —
    :func:`app.services.content_generator.bank_ideas` and
    :func:`app.services.content_generator.prune_ideas` — rather than inserted
    directly. The two rules those apply were written for a producer that
    repeats itself, and a build job is the producer that repeats *most*: a
    retried pipeline files its "v2.1 went out" a second time, and a pipeline
    that fires per commit files one an hour all day. A repeat answers 200 with
    the idea already open rather than adding a copy; the queue is held to the
    same cap the scan keeps it under, evicting the oldest unused idea past it.
    """
    headline = payload.headline.strip()
    existing = content_generator.restated_idea(db, key.project_id, headline)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _idea_out(existing)

    banked = content_generator.bank_ideas(
        db,
        key.project_id,
        [
            content_generator.Idea(
                content_type=payload.content_type,
                headline=headline,
                rationale=payload.rationale.strip(),
            )
        ],
        source={"kind": "api_key", "key_id": key.id, "prefix": key.prefix},
    )
    content_generator.prune_ideas(db, key.project_id)
    db.commit()
    # ``bank_ideas`` was handed one idea it had just been told was new, so it
    # inserted one row — but the prune runs on ``created_at`` and a cap of zero
    # can take back the row this request added. Re-read rather than assume.
    idea = db.get(ContentIdea, banked[0].id) if banked else None
    if idea is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The project's idea queue is full and nothing older could go.",
        )
    return _idea_out(idea)


def _idea_out(idea: ContentIdea) -> MachineIdeaOut:
    return MachineIdeaOut(
        id=idea.id,
        headline=idea.headline,
        content_type=idea.content_type.value,
        status="used" if idea.used_content_id else "open",
        created_at=idea.created_at,
    )


@router.get(
    "/analytics",
    response_model=MachineAnalyticsOut,
    summary="The project's numbers",
    responses=errors(*MACHINE_ERRORS, status.HTTP_403_FORBIDDEN),
)
@limiter.limit(settings.rate_limit_machine_api, key_func=api_key_key)
def machine_analytics(
    request: Request,
    response: Response,
    key: ApiKey = Depends(require_scope(ApiKeyScope.ANALYTICS_READ)),
    db: Session = Depends(get_db),
) -> MachineAnalyticsOut:
    """Published count and engagement totals for the key's project.

    The totals come from the *latest* snapshot per publication, not the sum of
    all of them. ``content_metrics`` is an append-only series — see
    :class:`app.models.metrics.ContentMetric` — so adding every row would count
    the same hundred views once per poll and produce a number that only ever
    rises, fast, and means nothing.
    """
    published = (
        db.scalar(
            select(func.count(Content.id)).where(
                Content.project_id == key.project_id,
                Content.status == ContentStatus.PUBLISHED,
            )
        )
        or 0
    )

    pub_sub = (
        select(Publication.id)
        .join(Content, Content.id == Publication.content_id)
        .where(
            Content.project_id == key.project_id,
            Publication.status == PublicationStatus.PUBLISHED,
        )
        .subquery()
    )

    platforms = sorted(
        {
            row[0].value
            for row in db.execute(
                select(Publication.platform)
                .where(Publication.id.in_(select(pub_sub.c.id)))
                .group_by(Publication.platform)
            ).all()
        }
    )

    # The newest snapshot per publication, entirely in SQL: group to find each
    # one's latest capture, then join back for that row's counters.
    latest = (
        select(
            ContentMetric.publication_id.label("pub_id"),
            func.max(ContentMetric.captured_at).label("captured"),
        )
        .where(ContentMetric.publication_id.in_(select(pub_sub.c.id)))
        .group_by(ContentMetric.publication_id)
        .subquery()
    )

    views = 0
    engagement = 0
    for metric in db.scalars(
        select(ContentMetric).join(
            latest,
            (ContentMetric.publication_id == latest.c.pub_id)
            & (ContentMetric.captured_at == latest.c.captured),
        )
    ):
        views += metric.views or 0
        engagement += metric.engagement

    return MachineAnalyticsOut(
        project_id=key.project_id,
        published_count=published,
        total_views=views,
        total_engagement=engagement,
        platforms=platforms,
    )


__all__ = ["MACHINE_ERRORS", "MAX_MACHINE_PAGE", "router"]
