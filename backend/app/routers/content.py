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
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.schemas.content import (
    BulkContentIn,
    BulkFailureOut,
    BulkPublishIn,
    BulkResultOut,
    ContentCreate,
    ContentDetail,
    ContentOut,
    ContentUpdate,
    GenerateRequest,
    HeadlineApplyIn,
    HeadlineVariantsOut,
    HeadlineWindowOut,
    HeadlineWinnerOut,
    InlineEditIn,
    InlineEditOut,
    InternalLinkSuggestionOut,
    LinkCheckOut,
    LinkStatusOut,
    PublicationOut,
    PublishRequestIn,
    RepurposeOut,
    ScheduleContentIn,
    SeoIssueOut,
    SlotOut,
    SocialCardsOut,
)
from app.services import (
    content_generator,
    content_pipeline,
    formats,
    github_client,
    headlines,
    inline_edit,
    link_check,
    publishers,
    publishing_service,
    repurpose,
    scheduling,
    seo,
    social_cards,
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
        content_format=formats.format_of(content.content_type).value,
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
        format_issues=formats.problems(content.body_markdown, content.content_type),
    )


def _commit_content(db: Session, content: Content) -> None:
    """Add and commit, retrying once on a slug collision.

    ``unique_content_slug`` prevents most collisions, but two concurrent
    requests can race past the check. The database-level unique constraint
    catches the loser; we regenerate the slug and retry rather than surfacing a
    500. The retry appends random hex rather than re-running the count query,
    which would be open to the same race a second time.
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
    # ``ge=1`` as well as ``le``: every other paginated endpoint here bounds
    # both ends, and this one bounded only the top. A negative ``limit`` reaches
    # SQLAlchemy's ``.limit()`` verbatim, and the two databases disagree about
    # what that means — SQLite reads ``LIMIT -1`` as "no limit" and returns the
    # caller's entire content table past the 500 cap, while Postgres refuses it
    # outright and the request becomes a 500.
    limit: int = Query(default=100, ge=1, le=500),
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


@router.post("/bulk/approve", response_model=BulkResultOut)
def bulk_approve_content(
    payload: BulkContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Approve many review-queue pieces in one call, skipping any that can't be.

    Each approved piece is released exactly as the single-item endpoint releases
    it — the two must not disagree about what Approve means, and the batch is
    the button the review queue actually offers.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    approved: list[Content] = []
    for content_id in payload.content_ids:
        content = db.get(Content, content_id)
        if content is None or content.project.user_id != user.id:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        if content.status == ContentStatus.PUBLISHED:
            failed.append(
                BulkFailureOut(content_id=content_id, reason="Already published")
            )
            continue
        content.status = ContentStatus.APPROVED
        approved.append(content)
        succeeded.append(content_id)
    db.commit()
    # After the commit, so a release that dispatches a worker cannot hand it a
    # row this request has not written yet.
    for content in approved:
        content_pipeline.release_approved(db, content)
    return BulkResultOut(succeeded=succeeded, failed=failed)


@router.post("/bulk/reject", response_model=BulkResultOut)
def bulk_reject_content(
    payload: BulkContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Archive many review-queue pieces in one call.

    There is no ``rejected`` status — archiving is the same "not going out"
    outcome a human hits one at a time from the editor, just batched.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    for content_id in payload.content_ids:
        content = db.get(Content, content_id)
        if content is None or content.project.user_id != user.id:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        if content.status == ContentStatus.PUBLISHED:
            failed.append(
                BulkFailureOut(content_id=content_id, reason="Already published")
            )
            continue
        content.status = ContentStatus.ARCHIVED
        succeeded.append(content_id)
    db.commit()
    return BulkResultOut(succeeded=succeeded, failed=failed)


@router.post("/bulk/publish", response_model=BulkResultOut)
def bulk_publish_content(
    payload: BulkPublishIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Queue many pieces for the same platform(s) in one call.

    Each piece is validated independently through the same checks as the
    single-item publish (adapter implemented, platform connected, no dead
    links) — one bad piece in the batch fails on its own and does not block
    the rest.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    single = PublishRequestIn(
        platforms=payload.platforms,
        scheduled_for=payload.scheduled_for,
        as_draft=payload.as_draft,
        allow_broken_links=payload.allow_broken_links,
    )
    for content_id in payload.content_ids:
        content = db.get(Content, content_id)
        if content is None or content.project.user_id != user.id:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        try:
            _queue_publish(content, single, db)
        except _PublishError as exc:
            failed.append(BulkFailureOut(content_id=content_id, reason=exc.detail))
            continue
        succeeded.append(content_id)
    return BulkResultOut(succeeded=succeeded, failed=failed)


@router.get("/{content_id}/internal-links", response_model=list[InternalLinkSuggestionOut])
def internal_link_suggestions(
    content_id: int,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[InternalLinkSuggestionOut]:
    """Other published posts in this project worth linking to, by keyword overlap."""
    content = _owned_content(content_id, db, user)
    candidates = db.scalars(
        select(Content).where(
            Content.project_id == content.project_id,
            Content.id != content.id,
            Content.status == ContentStatus.PUBLISHED,
        )
    )
    suggestions = seo.suggest_internal_links(
        keywords=content.keywords,
        focus_keyword=content.focus_keyword,
        candidates=[
            {
                "content_id": c.id,
                "title": c.title,
                "slug": c.slug,
                "keywords": c.keywords,
                "focus_keyword": c.focus_keyword,
                "canonical_url": c.canonical_url,
            }
            for c in candidates
        ],
        limit=limit,
    )
    return [InternalLinkSuggestionOut(**s) for s in suggestions]


@router.get("/{content_id}/social", response_model=SocialCardsOut)
def social_cards_preview(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SocialCardsOut:
    """How this piece unfurls when its link is pasted into a feed.

    GET and free: the service is pure, so unlike ``/repurpose`` there is no
    model call to ration. The editor computes the same previews locally while
    you type; this endpoint is the authority for the meta tags themselves,
    which the editor offers for pasting into a hand-rolled site.
    """
    content = _owned_content(content_id, db, user)
    # The address the card will actually carry: the canonical if the piece
    # names one, otherwise wherever it first went live. Empty is fine — it
    # only decides the domain line on the card.
    live = next(
        (
            p.external_url
            for p in content.publications
            if p.status == PublicationStatus.PUBLISHED and p.external_url
        ),
        None,
    )
    result = social_cards.summary(
        title=content.title,
        url=content.canonical_url or live or "",
        meta_description=content.meta_description,
        excerpt=content.excerpt,
        body_markdown=content.body_markdown,
        cover_image_url=content.cover_image_url,
        site_name=content.project.name if content.project else "",
        tags=list(content.tags or []),
    )
    return SocialCardsOut(**result)


@router.post("/{content_id}/repurpose", response_model=RepurposeOut)
def repurpose_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RepurposeOut:
    """Draft a Twitter thread and a LinkedIn post from this piece.

    POST, not GET, and runs inline like ``/generate``: this costs an LLM call
    and nothing is persisted, so the caller gets a fresh set of snippets every
    time — there is no cached result to invalidate.
    """
    content = _owned_content(content_id, db, user)
    result = repurpose.generate(content, content.project)
    return RepurposeOut(
        twitter_thread=result.twitter_thread,
        linkedin_post=result.linkedin_post,
        provider=result.provider,
        model=result.model,
        is_fallback=result.is_fallback,
    )


@router.post("/{content_id}/edit", response_model=InlineEditOut)
def edit_passage(
    content_id: int,
    payload: InlineEditIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InlineEditOut:
    """Rewrite, shorten, expand or retone one passage of a draft.

    Nothing is persisted, for the same reason ``/repurpose`` and
    ``/headlines`` persist nothing: the author decides whether the model
    improved anything. The editor splices the replacement into the textarea it
    already holds, which keeps the browser's own undo working — a server-side
    apply would need a revision model to be safe, and this needs one less.

    The selection is required to appear verbatim in the stored body. That check
    is worth more than it looks: it is what stops a stale editor from asking
    for an edit to a paragraph that no longer exists and splicing the answer
    back over whatever is there now, and it means the endpoint cannot be used
    to run arbitrary text through the provider chain on the account's quota.
    """
    content = _owned_content(content_id, db, user)
    selection = payload.selection

    if selection not in (content.body_markdown or ""):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That passage is not in the saved draft. Save your changes "
                "first, then select it again."
            ),
        )

    try:
        result = inline_edit.edit(
            body_markdown=content.body_markdown,
            selection=selection,
            operation=payload.operation,
            title=content.title,
            project=content.project,
            tone=payload.tone,
        )
    except inline_edit.EditUnavailable as exc:
        # 503, not 500: nothing is broken here, a dependency is unavailable or
        # answered with something unusable, and trying again is the right move.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return InlineEditOut(
        replacement=result.replacement,
        operation=result.operation,
        provider=result.provider,
        model=result.model,
    )


@router.post("/{content_id}/headlines", response_model=HeadlineVariantsOut)
def generate_headline_variants(
    content_id: int,
    count: int = Query(default=4, ge=2, le=6),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HeadlineVariantsOut:
    """Draft alternative headlines for this piece.

    Nothing is applied or persisted here — POST /{content_id}/headlines/apply
    is the separate step that actually swaps the live title.
    """
    content = _owned_content(content_id, db, user)
    result = headlines.generate_variants(content, content.project, count=count)
    return HeadlineVariantsOut(
        variants=result.variants,
        provider=result.provider,
        model=result.model,
        is_fallback=result.is_fallback,
    )


@router.post("/{content_id}/headlines/apply", response_model=ContentOut)
def apply_content_headline(
    content_id: int,
    payload: HeadlineApplyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentOut:
    """Swap the live title, whether or not this piece is already published.

    Unlike ``PATCH /{content_id}``, this is allowed after publish — testing
    headlines on live content is the point. The slug (and so the URL) is left
    untouched; only the display title changes.
    """
    content = _owned_content(content_id, db, user)
    headlines.apply_headline(content, payload.title)
    db.commit()
    db.refresh(content)
    return _to_out(content)


@router.get("/{content_id}/headlines/performance", response_model=list[HeadlineWindowOut])
def content_headline_performance(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[HeadlineWindowOut]:
    """What each headline (including the current one) earned while it was live."""
    content = _owned_content(content_id, db, user)
    return [
        HeadlineWindowOut(**w.as_dict()) for w in headlines.performance(content, db)
    ]


def _winner_out(verdict: headlines.Winner, *, applied: bool = False) -> HeadlineWinnerOut:
    return HeadlineWinnerOut(
        title=verdict.title,
        confident=verdict.confident,
        reason=verdict.reason,
        score=verdict.score,
        current_score=verdict.current_score,
        ranked=[HeadlineWindowOut(**w.as_dict()) for w in verdict.ranked],
        applied=applied,
    )


@router.get("/{content_id}/headlines/winner", response_model=HeadlineWinnerOut)
def content_headline_winner(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HeadlineWinnerOut:
    """Which headline is winning, and whether the lead is worth acting on.

    Read-only: nothing is applied here, so the answer can be shown next to the
    headline without committing to it.
    """
    content = _owned_content(content_id, db, user)
    return _winner_out(headlines.pick_winner(headlines.performance(content, db)))


@router.post("/{content_id}/headlines/auto-select", response_model=HeadlineWinnerOut)
def apply_headline_winner(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HeadlineWinnerOut:
    """Swap in the winning headline, if there is a confident one.

    Not an error when nothing changes — "the headline live now is the best one"
    is a successful answer to "pick the winner", and the reason says which case
    it was. The same decision the beat sweep makes for projects that opted in,
    available on demand for those that did not.
    """
    content = _owned_content(content_id, db, user)
    verdict, applied = headlines.auto_select(content, db)
    if applied:
        db.commit()
        db.refresh(content)
    return _winner_out(verdict, applied=applied)


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


def _manual_source(user: User, campaign_key: str | None) -> dict:
    """The ``source`` blob for a hand-written piece.

    ``campaign_key`` is only present when the caller supplied one, so a piece
    written in the UI keeps exactly the shape it always had.
    """
    source: dict = {"kind": "manual", "user_id": user.id}
    if campaign_key:
        source["campaign_key"] = campaign_key
    return source


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
        source=_manual_source(user, payload.campaign_key),
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

    # The one status a published piece may take. Moving it back to draft or
    # review says it is not published when it is still live everywhere it went,
    # and the analytics and the calendar both read this column. Archiving is
    # different: it means "stop showing me this", not "this never went out".
    if (
        content.status == ContentStatus.PUBLISHED
        and data.get("status") not in (None, ContentStatus.ARCHIVED)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is live on the platforms. Archive it to hide it "
            "from Herald, or unpublish it there first.",
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
    # Approving through here means the same thing as approving through the
    # button — a scripted caller that PATCHes the status should not need to know
    # about a second endpoint to get its piece published.
    if data.get("status") == ContentStatus.APPROVED:
        content_pipeline.release_approved(db, content)
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
    """Mark a piece approved, and release it if the project publishes on its own.

    Approving is what un-blocks publication for a project on ``auto``: the
    autopilot would have published this itself and only diverted it to review
    because a quality gate failed, so a human saying yes is that gate clearing.
    See :func:`app.services.content_pipeline.release_approved` for the
    conditions — nothing is queued for a project on ``off`` or ``draft``, or for
    a piece that already has publications, and approving stays a status change
    there.
    """
    content = _owned_content(content_id, db, user)
    if content.status == ContentStatus.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already published"
        )
    content.status = ContentStatus.APPROVED
    db.commit()
    content_pipeline.release_approved(db, content)
    db.refresh(content)
    return _to_out(content)


class _PublishError(Exception):
    """A validation failure from :func:`_queue_publish`.

    Carries the HTTP status the single-item endpoint should raise; the bulk
    endpoint reads ``.detail`` and records it against that one item instead.
    """

    def __init__(self, detail: str, status_code: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _queue_publish(
    content: Content, payload: PublishRequestIn, db: Session
) -> list[Publication]:
    """Validate and queue a piece for one or more platforms.

    Shared by the single-item and bulk publish endpoints so both apply the
    exact same adapter/connection/link checks. Queuing is synchronous and
    cheap; the publishing itself is a worker's job (or runs inline when
    ``CELERY_ENABLED`` is off).
    """
    try:
        when = scheduling.normalize(payload.scheduled_for)
    except scheduling.ScheduleError as exc:
        # A time in the past would otherwise be picked up by the very next
        # sweep — "publish now" wearing the costume of a schedule.
        raise _PublishError(str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY) from exc

    unimplemented = [
        p.value for p in payload.platforms if not publishers.get_adapter(p).implemented
    ]
    if unimplemented:
        raise _PublishError(
            f"No finished adapter for: {', '.join(unimplemented)}. "
            f"Publishing works for: "
            f"{', '.join(p.value for p in publishers.implemented_platforms())}."
        )

    missing = [
        p.value
        for p in payload.platforms
        if p.value not in content.project.user.connected_platforms
    ]
    if missing:
        raise _PublishError(
            f"Not connected to: {', '.join(missing)}. Add credentials in Settings."
        )

    # Last cheap chance to catch a URL the model invented. Only a definitive 404
    # or 410 stops the publish — see app.services.link_check on why a timeout
    # must not.
    if settings.link_check_enabled and not payload.allow_broken_links:
        dead = link_check.broken(_check_content_links(content))
        if dead:
            raise _PublishError(
                f"{len(dead)} link(s) in this post are dead: "
                + "; ".join(f"{s.url} ({s.http_status or 'unreachable'})" for s in dead)
                + ". Fix them, or publish anyway with allow_broken_links.",
                status_code=status.HTTP_409_CONFLICT,
            )

    publications = publishing_service.queue(
        db,
        content,
        list(payload.platforms),
        scheduled_for=when,
        as_draft=payload.as_draft,
    )

    if content.status in (ContentStatus.DRAFT, ContentStatus.REVIEW):
        content.status = ContentStatus.APPROVED
    content.scheduled_for = when
    db.commit()

    # Nothing scheduled goes out now. Scheduled work waits for the beat task.
    #
    # "Nothing scheduled" is per publication, not per request: an immediate
    # cross-post still leaves the syndicated copies parked behind the canonical
    # by ``publishing_service._syndication_schedule``, and dispatching those
    # here would ask a worker to publish them the moment the original went out.
    if when is None:
        _dispatch([p.id for p in publications if p.scheduled_for is None])

    for publication in publications:
        db.refresh(publication)
    return publications


@router.post("/{content_id}/publish", response_model=list[PublicationOut])
def publish_content(
    content_id: int,
    payload: PublishRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Queue a piece for one or more platforms.

    The response is the queue state, not the outcome — the caller polls the
    publications for that.
    """
    content = _owned_content(content_id, db, user)
    try:
        publications = _queue_publish(content, payload, db)
    except _PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return [PublicationOut.model_validate(p) for p in publications]


def _schedulable_platforms(content: Content) -> list[Platform]:
    """The platforms this piece is queued for and could still be moved.

    Anything already live is excluded: its time is not in the future any more,
    and rescheduling it would only mean posting it twice.
    """
    return [
        p.platform
        for p in content.publications
        if p.status != PublicationStatus.PUBLISHED
    ]


def _canonical_platform(content: Content) -> Platform | None:
    project = content.project
    if project is None or not project.auto_canonical:
        return None
    return project.canonical_platform


@router.get("/{content_id}/schedule/suggestions", response_model=list[SlotOut])
def schedule_suggestions(
    content_id: int,
    platforms: list[Platform] | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SlotOut]:
    """When Herald would put this out, and why. Nothing is queued or changed.

    Answering this before the user commits is the point: a proposed Tuesday
    13:00 UTC they can override is more useful than one applied silently.
    """
    content = _owned_content(content_id, db, user)
    wanted = (
        list(platforms or [])
        or _schedulable_platforms(content)
        or [Platform(p) for p in user.connected_platforms]
    )
    if not wanted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name the platforms to suggest slots for — this piece is not "
            "queued anywhere and no platform is connected.",
        )

    slots = scheduling.optimal_slots(
        db, user.id, list(wanted), canonical=_canonical_platform(content)
    )
    return [SlotOut(**slot.as_dict()) for slot in slots]


@router.post("/{content_id}/schedule", response_model=list[PublicationOut])
def schedule_content(
    content_id: int,
    payload: ScheduleContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Put a piece on the calendar for a specific time, or let Herald pick one.

    Runs the same adapter, connection and dead-link checks as an immediate
    publish — a schedule that passes validation now and fails at 3am because
    the platform was never connected is the worst of both worlds.
    """
    content = _owned_content(content_id, db, user)

    if payload.optimize and payload.scheduled_for is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Give a time or ask Herald to pick one — not both.",
        )
    if not payload.optimize and payload.scheduled_for is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Give a scheduled_for, or set optimize to have Herald pick "
            "one. Use POST /content/{id}/publish to go out now.",
        )

    platforms = list(payload.platforms or _schedulable_platforms(content))
    if not platforms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This piece is not queued anywhere yet — name the platforms "
            "to schedule it for.",
        )

    slots: list[scheduling.Slot] = []
    when = payload.scheduled_for
    if payload.optimize:
        slots = scheduling.optimal_slots(
            db, user.id, platforms, canonical=_canonical_platform(content)
        )
        # The piece's own time is the first thing to go out; each platform
        # keeps its own below.
        when = slots[0].when

    try:
        publications = _queue_publish(
            content,
            PublishRequestIn(
                platforms=platforms,
                scheduled_for=when,
                as_draft=payload.as_draft,
            ),
            db,
        )
    except _PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if slots:
        per_platform = {slot.platform: slot.when for slot in slots}
        for publication in publications:
            if publication.status == PublicationStatus.PUBLISHED:
                continue
            publication.scheduled_for = per_platform.get(
                publication.platform, publication.scheduled_for
            )
            publication.status = PublicationStatus.SCHEDULED
        db.commit()
        for publication in publications:
            db.refresh(publication)

    return [PublicationOut.model_validate(p) for p in publications]


@router.delete("/{content_id}/schedule", response_model=list[PublicationOut])
def unschedule_content(
    content_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Take a piece off the calendar without publishing it.

    Scheduled rows are cancelled rather than returned to ``pending``: pending
    means "go out on the next sweep", which is the opposite of what somebody
    clicking unschedule asked for. Re-publishing later re-arms the same rows.
    """
    content = _owned_content(content_id, db, user)
    targets = [
        p for p in content.publications if p.status == PublicationStatus.SCHEDULED
    ]

    for publication in targets:
        publication.status = PublicationStatus.CANCELLED
        publication.scheduled_for = None
    content.scheduled_for = None
    db.commit()
    for publication in targets:
        db.refresh(publication)
    return [PublicationOut.model_validate(p) for p in targets]


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
