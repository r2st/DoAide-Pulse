"""Project registry: register what Herald should write about."""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload, load_only, selectinload

from app.config import settings
from app.database import get_db
from app.deps import ListOffset, RowId, get_current_user, owned_project
from app.models.content import Content, ContentIdea, ContentStatus
from app.models.mixins import elapsed_ms
from app.models.project import Project, slugify
from app.models.publication import Publication
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.routers._patch import reject_nulls
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.schemas.project import (
    IdeaOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RepoActivityOut,
)
from app.services import content_generator, github_client, rss

logger = logging.getLogger(__name__)

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
            "autopilot_mode", "autopilot_platforms",
            "autopilot_min_interval_hours", "auto_canonical",
            "canonical_platform", "auto_headline_winner",
            "engagement_threshold",
            "utm_enabled", "utm_campaign",
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
            "autopilot_blocked_reason": project.autopilot_blocked_reason,
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


@router.get(
    "",
    response_model=list[ProjectOut],
    summary="List your projects",
    responses=errors(*AUTHENTICATED),
)
def list_projects(
    response: Response,
    # Nothing caps projects per account, so this read was bounded only by how
    # many the caller had made. Three things then grew with it: the response,
    # the `selectinload` of every trigger on every project, and the `IN` clause
    # `_batch_counts` builds from the ids — and that last one is a hard failure
    # rather than a slow one, because Postgres refuses a statement with more
    # than 65535 bind parameters. Same bounds and the same X-Total-Count as
    # every other listing here; see `list_content` on why `ge=1` matters.
    limit: int = Query(default=100, ge=1, le=500),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ProjectOut]:
    """Every project on this account, alphabetically.

    Paged, and the total is in ``X-Total-Count`` rather than the body — the
    body is a plain array so a client can hand it straight to a list view.

    ``autopilot_blocked_reason`` is filled in here, which is the field worth
    reading first: a project with the autopilot on and no way for it to fire
    looks identical to a working one until this says otherwise.
    """
    base = select(Project).where(Project.user_id == user.id)

    total = db.scalar(
        select(func.count()).select_from(base.with_only_columns(Project.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)

    projects = list(
        db.scalars(
            base
            # `autopilot_blocked_reason` reads `project.triggers`; without this
            # the list page emits one extra query per project to find out.
            .options(selectinload(Project.triggers))
            # Tiebroken by id: only ``(user_id, slug)`` is unique, and the slug
            # is uniquified precisely *because* two projects on one account can
            # share a name. Paging alphabetically over a name that repeats is
            # paging over a sort the database may break either way.
            .order_by(Project.name, Project.id)
            .offset(offset)
            .limit(limit)
        )
    )
    counts = _batch_counts(db, [p.id for p in projects])
    return [
        _to_out(p, content_count=counts.get(p.id, (0, 0))[0],
                published_count=counts.get(p.id, (0, 0))[1])
        for p in projects
    ]


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a project",
    responses=errors(*AUTHENTICATED),
)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    """Add a project for Herald to write about.

    The slug is derived from the name and made unique within the account; it is
    not settable, because it appears in the public feed URL and a user-chosen
    one that collides has no good resolution.

    Only ``name`` is required. Everything else — the repo URL, the tone, the
    keywords, the autopilot settings — sharpens what gets written and can be
    filled in later.
    """
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


@router.get(
    "/{project_id}",
    response_model=ProjectOut,
    summary="One project",
    responses=errors(*OWNED),
)
def get_project(
    project_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    """One project, with its content counters and autopilot state."""
    return _to_out(owned_project(project_id, db, user), db=db)


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    summary="Change a project's settings",
    responses=errors(*OWNED, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def update_project(
    project_id: RowId,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectOut:
    """Change any of a project's settings.

    A field left out is left alone. A field sent as ``null`` is an instruction
    to clear it, which is refused with a 422 for the columns that cannot hold
    one rather than failing at commit time as a 500 — see
    :mod:`app.routers._patch`.

    Renaming re-slugs the project, and so changes its public feed URL. A PATCH
    that sends the same name does not.
    """
    project = owned_project(project_id, db, user)
    data = payload.model_dump(exclude_unset=True)
    reject_nulls(Project, data)

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


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project and everything under it",
    responses=errors(*OWNED),
)
def delete_project(
    project_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Delete a project, its content, its triggers and its publication history.

    This one really deletes. Content already live on a platform stays live —
    Herald cannot unpublish it, and this removes only Herald's record of it.
    """
    project = owned_project(project_id, db, user)
    # Counted *before* the delete and logged before the commit: this is the one
    # endpoint in Herald whose effect cannot be inspected afterwards. The cascade
    # removes the content, the publication history and the triggers, so once it
    # has run there is nothing left to ask what it took — and the posts it was
    # the only record of are still live on the platforms. Two COUNTs against a
    # statement that is about to delete those rows anyway.
    pieces = db.scalar(
        select(func.count(Content.id)).where(Content.project_id == project.id)
    )
    publications = db.scalar(
        select(func.count(Publication.id))
        .join(Content, Content.id == Publication.content_id)
        .where(Content.project_id == project.id)
    )
    logger.warning(
        "project %s (%s) deleted by user %s — %s piece(s) and %s publication(s) "
        "went with it; anything already live stays live",
        project.id,
        project.slug,
        user.id,
        pieces or 0,
        publications or 0,
    )
    db.delete(project)
    db.commit()


@router.post(
    "/{project_id}/scan",
    response_model=RepoActivityOut,
    summary="Scan the linked repository for new work",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        *OWNED,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_502_BAD_GATEWAY,
    ),
)
@limiter.limit(settings.rate_limit_repo_scan, key_func=account_key)
def scan_repo(
    project_id: RowId,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RepoActivityOut:
    """Fetch what has shipped since the last scan, and move the watermark.

    Moving the watermark here is deliberate: a manual scan is the user saying
    "I've seen this", so the autopilot should not then write about the same
    commits an hour later.

    Rate-limited per account because the quota it spends is not the account's.
    ``GITHUB_TOKEN`` is one token for the whole install, and once its hourly
    budget is gone every project's scan — and the autopilot's own polling —
    answers 403 until the window rolls over. The 429 below reports GitHub
    saying no; this stops one caller getting it said to everybody.
    """
    project = owned_project(project_id, db, user)
    full_name = project.repo_full_name
    if not full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This project has no GitHub repo URL to scan.",
        )

    started = time.monotonic()
    try:
        activity = github_client.fetch_activity(
            full_name,
            since_sha=project.last_seen_commit_sha,
            since_tag=project.last_seen_release_tag,
        )
    except github_client.GitHubRateLimited as exc:
        # GitHub's own backoff, passed straight through. A 429 whose body says
        # "wait 47 seconds" in prose and whose headers say nothing is a 429 a
        # client has to guess at, and the guess is what got us throttled.
        headers = (
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None
            else None
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers=headers,
        ) from exc
    except github_client.GitHubError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    project.last_seen_commit_sha = activity.head_sha
    project.last_seen_release_tag = activity.latest_tag
    # Through the same model method the beat sweep uses. A hand-run scan is a
    # scan: it spends the same GitHub quota, takes the same time, and moves the
    # same watermark, so leaving it out of the counter would make the frequency
    # on `/api/v1/metrics` disagree with what the project page shows for any
    # project somebody had been clicking Scan on.
    project.record_scan(duration_ms=elapsed_ms(started))
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


@router.get("/{project_id}/feed.xml", include_in_schema=False)
@limiter.limit(settings.rate_limit_public_feed)
def project_feed(
    project_id: RowId,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Public RSS feed of this project's published content.

    Unauthenticated on purpose: an RSS reader has no bearer token to send, and
    every item here is already live wherever it was published. Only
    ``PUBLISHED`` content is ever included — drafts and the review queue never
    reach this endpoint regardless of who asks.

    Rate-limited because "no token required" and "free to serve" are different
    claims: this is two queries and an XML render, the project id is a small
    integer anyone can walk, and it was the one anonymous endpoint in the API
    that would answer as fast as it was asked.

    A project with nothing published answers 404, not an empty feed. The
    channel block is built from ``name``, ``description`` and ``live_url``,
    which are the project's own metadata and not published content — serving
    them for an empty feed turned a walkable integer id into an inventory of
    every project on the instance, including private ones that had never
    published anywhere. "Only published content is included" has to cover the
    channel, not just the items.
    """
    # Joined to the owner rather than fetched by id, because a deactivated
    # account must not still be publishing. Deactivation is how an account is
    # switched off in Herald: its tokens stop working, its sweeps skip it, its
    # inbound webhooks write nothing, its approved content is not released, its
    # preview links stop resolving. This feed was the last thing Herald kept
    # doing on a switched-off account's behalf — on Herald's own domain, from
    # Herald's own render of the project's metadata, to every subscriber on a
    # timer, with no expiry to run out the way a preview link's does.
    #
    # A join and not a walk through ``project.user``: the owner decides whether
    # there is a feed at all, and the walk would be a second query on the one
    # endpoint whose query count is pinned (``test_n_plus_one``).
    #
    # ``Project.is_active`` is deliberately *not* checked. Pausing a project
    # stops Herald writing new pieces for it; it is not a request to retract
    # the feed of what it already published, and the owner is still signed in
    # and able to say so directly.
    project = db.scalar(
        select(Project)
        .join(User, User.id == Project.user_id)
        .where(Project.id == project_id, User.is_active.is_(True))
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    items = list(
        db.scalars(
            select(Content)
            # ``rss.build_feed`` reads a title, an excerpt, a slug, a canonical
            # URL and a date. It never looks at a publication, but
            # ``Content.publications`` is ``lazy="selectin"`` for the content
            # list's sake, so serving this feed ran two queries where one is
            # needed — and this is the unauthenticated endpoint, the one that
            # gets polled by every reader on a timer.
            #
            # The same sentence decides the columns. Suppressing the second
            # query left the first one selecting whole ``Content`` rows —
            # ``FEED_ITEM_LIMIT`` is 50, so every poll of a busy project's feed
            # read fifty article bodies to render fifty titles and excerpts, and
            # a query-count assertion cannot see a byte of that.
            #
            # ``load_only`` defers everything not named, so a field added to
            # ``build_feed`` without being added here would come back as a
            # SELECT per item. ``test_the_rss_feed_does_not_read_an_article_body``
            # pins both halves: no body, and still one query.
            .options(
                lazyload(Content.publications),
                load_only(
                    Content.title,
                    Content.slug,
                    Content.excerpt,
                    Content.meta_description,
                    Content.canonical_url,
                    Content.published_at,
                ),
            )
            .where(Content.project_id == project.id, Content.status == ContentStatus.PUBLISHED)
            # ``id`` breaks the tie, because a sort with ties is not a sort and
            # this one has a LIMIT under it. Two pieces published in the same
            # instant leave the database free to order them either way, and at
            # the ``FEED_ITEM_LIMIT`` boundary that decides which of them is in
            # the feed at all — a reader watching an item appear on one poll,
            # vanish on the next and come back as unread on the one after.
            # Descending to match the dates: newest first, all the way down.
            .order_by(Content.published_at.desc(), Content.id.desc())
            .limit(rss.FEED_ITEM_LIMIT)
        )
    )
    if not items:
        # Same status and same detail as a project id that does not exist:
        # a caller must not be able to tell the two apart.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Built from configuration rather than from ``str(request.url)``.
    #
    # ``atom:link rel="self"`` is the feed telling a subscriber where the feed
    # is, which makes it the same kind of value as the webhook URL in
    # ``app.routers.triggers`` — a URL Herald hands to somebody else's system —
    # and ``api_base_url`` is what this codebase already uses for those. The
    # request URL is not that value for two separate reasons:
    #
    # * **The scheme is the proxy's, not the app's.** Herald runs behind Caddy,
    #   which terminates TLS and forwards plain HTTP to a bridge address.
    #   Uvicorn only honours ``X-Forwarded-Proto`` from ``forwarded_allow_ips``,
    #   which does not include that address, so ``request.url.scheme`` is
    #   ``http`` for every request that arrived over ``https``. The feed was
    #   advertising itself at a scheme the site does not serve — on a host with
    #   HSTS preloaded, no less.
    # * **The query string is the caller's.** ``?utm_source=reader`` is not part
    #   of where the feed lives, and echoing it back put a value nobody vouched
    #   for into an XML attribute. The canonical URL has no query string, so now
    #   neither does the self link.
    self_url = (
        f"{settings.api_base_url}{settings.api_v1_prefix}"
        f"/projects/{project.id}/feed.xml"
    )
    xml = rss.build_feed(project, items, self_url=self_url)
    return Response(content=xml, media_type="application/rss+xml")


#: The spellings pydantic parses as a true ``bool`` query param. Mirrored here
#: rather than re-derived, because :func:`_not_refreshing` has to reach the same
#: verdict as FastAPI does about the very same string — a limit that exempts
#: ``?refresh=on`` while the endpoint honours it is an unlimited endpoint.
_TRUTHY = frozenset({"1", "t", "true", "y", "yes", "on"})


def _not_refreshing(request: Request) -> bool:
    """Whether this ``/ideas`` call is the free read rather than the model call.

    The limit below covers one endpoint with two costs. Without ``refresh`` it
    is two queries the projects page runs on every visit; with it, it is an LLM
    call. Limiting both at the model call's budget would throttle a page load;
    limiting neither leaves the model call open. So the limit is declared on the
    endpoint and exempted for the cheap half.
    """
    return request.query_params.get("refresh", "").strip().lower() not in _TRUTHY


@router.get(
    "/{project_id}/ideas",
    response_model=list[IdeaOut],
    summary="Subjects worth writing about",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(
    settings.rate_limit_ai_generate, key_func=account_key, exempt_when=_not_refreshing
)
def list_ideas(
    project_id: RowId,
    request: Request,
    response: Response,
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[IdeaOut]:
    """Subjects worth writing about. ``refresh=true`` asks the model for more.

    Without ``refresh`` this is a cheap read of what the autopilot has already
    banked, so the projects page can show ideas without an LLM call per visit —
    and only the refreshing half counts against a rate limit, see
    :func:`_not_refreshing`.
    """
    project = owned_project(project_id, db, user)

    if refresh:
        # Deduplicated against what is already waiting, because the button is
        # pressed repeatedly by construction — a user who does not like the list
        # presses it again — and the model is asked at ``temperature=0.9`` with
        # the same project brief every time. Without this, three presses banked
        # three copies of whatever the model likes about this project, and the
        # twelve-row read below returned four ideas in twelve rows.
        content_generator.bank_ideas(
            db,
            project.id,
            content_generator.suggest_ideas(project),
            source={"kind": "manual_refresh"},
        )
        db.commit()

    rows = db.scalars(
        select(ContentIdea)
        .where(ContentIdea.project_id == project.id, ContentIdea.used_content_id.is_(None))
        .order_by(ContentIdea.created_at.desc())
        .limit(12)
    )
    return [IdeaOut.model_validate(row) for row in rows]
