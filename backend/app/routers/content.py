"""Content: generate, edit, review, approve, publish."""
from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, joinedload, selectinload

from app.config import settings
from app.database import get_db, refresh_all
from app.deps import ListOffset, RowId, get_current_user, owned_project
from app.models.content import Content, ContentIdea, ContentStatus, ContentType, unique_content_slug
from app.models.mixins import utcnow
from app.models.preview_link import PreviewLink
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.routers._patch import reject_nulls
from app.schemas.content import (
    ArchiveOldIn,
    ArchiveOldOut,
    BulkContentIn,
    BulkFailureOut,
    BulkPublishIn,
    BulkResultOut,
    ContentCreate,
    ContentDetail,
    ContentEngagementOut,
    ContentOut,
    ContentStatusIn,
    ContentUpdate,
    GenerateRequest,
    HeadlineApplyIn,
    HeadlineReachOut,
    HeadlineUnreachableOut,
    HeadlineVariantsOut,
    HeadlineWindowOut,
    HeadlineWinnerOut,
    InlineEditIn,
    InlineEditOut,
    InternalLinkSuggestionOut,
    LinkCheckOut,
    LinkStatusOut,
    PlatformCheckOut,
    PlatformChecksOut,
    PreflightFindingOut,
    PreviewLinkCreate,
    PreviewLinkOut,
    PublicationOut,
    PublicPreviewOut,
    PublishRequestIn,
    QualityOut,
    RepurposeOut,
    RetryResultOut,
    ScheduleContentIn,
    SeoIssueOut,
    SlotOut,
    SocialCardsOut,
)
from app.schemas.errors import AUTHENTICATED, OWNED, ErrorOut, errors
from app.services import (
    content_engagement,
    content_generator,
    content_pipeline,
    formats,
    github_client,
    headlines,
    inline_edit,
    link_check,
    platform_check,
    preview_links,
    publishers,
    publishing_service,
    quality,
    repurpose,
    revisions,
    scheduling,
    seo,
    social_cards,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/content", tags=["content"])

#: The publication states a retry means something for. ``published`` is refused
#: rather than skipped — retrying it would post a second copy — and everything
#: else here is a row that has stopped without having gone out. ``pending``,
#: ``scheduled`` and ``publishing`` are left alone by the batch retries: the
#: first two are already queued and the third is claimed by a worker that is
#: about to succeed, and re-arming that one is how a post goes out twice.
RETRYABLE = (
    PublicationStatus.FAILED,
    PublicationStatus.CANCELLED,
)

#: What an age-based sweep archives when the caller does not say. The two
#: statuses that accumulate without anybody deciding anything: a draft nobody
#: finished and a review-queue piece nobody judged. ``approved`` is left out on
#: purpose — somebody said yes to it, and a piece waiting on a schedule is not
#: abandoned — though a caller may name it explicitly.
_ARCHIVE_SWEEP_DEFAULT_STATUSES = (ContentStatus.DRAFT, ContentStatus.REVIEW)


def _owned_content(content_id: RowId, db: Session, user: User) -> Content:
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found"
        )
    return content


def _etag(content: Content) -> str:
    """This piece's current version as an HTTP entity tag.

    Quoted, because an entity tag is quoted — RFC 9110 §8.8.3 spells the grammar
    as ``DQUOTE *etagc DQUOTE`` and a bare ``4`` is not an ``entity-tag`` at all.
    A client that parses the header properly would refuse it; one that does not
    would echo the wrong thing back.
    """
    return f'"{content.version}"'


def _assert_if_match(content: Content, if_match: str | None) -> None:
    """Refuse the write if *if_match* names a version this piece has moved past.

    Absent header, no precondition. The alternative — requiring one — would
    break every client written before the field existed, including the ``curl``
    in the docs, and the failure would be a 428 on a request that is very often
    the only editor of that piece. What the header buys is available to whoever
    asks for it, which is the shape ``If-Match`` has in HTTP generally.

    ``*`` matches any existing representation, per RFC 9110 §13.1.1. It is
    always satisfied here: the piece was loaded before this ran, so a
    representation exists by construction.

    A list is accepted because the grammar is a list, and any member matching is
    a match. Anything that is not an entity tag is a 400 rather than a silent
    pass: the request asked for a guarantee in a spelling this server does not
    understand, and answering 200 would be claiming the guarantee was checked.
    """
    if if_match is None:
        return
    candidates = [tag.strip() for tag in if_match.split(",")]
    if not any(candidates):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match was sent empty. Send the piece's version as an "
            'entity tag — If-Match: "4" — or leave the header off.',
        )
    if "*" in candidates:
        return

    versions = set()
    for tag in candidates:
        # Weak tags are meaningless for If-Match (RFC 9110 §13.1.1 requires a
        # strong comparison), and Herald never mints one, so a `W/` prefix is
        # something else's idea of this tag. Refused rather than unwrapped.
        if len(tag) < 2 or not tag.startswith('"') or not tag.endswith('"'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"If-Match must be a quoted entity tag, not {tag!r}. Send "
                "the version from the piece you loaded — If-Match: \"4\".",
            )
        versions.add(tag[1:-1])

    if str(content.version) not in versions:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Somebody else has edited this piece since you loaded it "
            f"(you have version {'/'.join(sorted(versions))}, it is now "
            f"{content.version}). Reload it and reapply your changes — saving "
            "now would overwrite theirs.",
        )


def _is_live(content: Content) -> bool:
    """Whether any of this piece is readable on a platform right now.

    The edit freeze in :func:`update_content` asks this rather than reading the
    status column, because the column stops saying ``published`` before the post
    stops being live. Archiving is the case: it is the one edit a published
    piece accepts, it means "stop showing me this" rather than "this never went
    out", and it moves the status to ``archived`` — which took the piece out of
    the freeze while it was still up on Dev.to. Two calls then did what one was
    refused:

        PATCH {"body_markdown": ...}   -> 409, already published
        PATCH {"status": "archived"}   -> 200
        PATCH {"body_markdown": ...}   -> 200, and the slug moves with the title

    Leaving Herald's copy of a live post saying something the post does not,
    under a slug that is no longer the one the canonical link was published
    with. The reason the freeze gives — that editing here would not change what
    is live on the platforms — is exactly as true after archiving, so the
    question it asks is "did any of this go out", not "what does the column
    say".

    ``publications`` is ``lazy="selectin"`` and ``_owned_content`` has already
    loaded the piece, so this costs no query of its own.
    """
    return content.went_out


#: The statuses nothing is queued from. A piece in any of these has no armed
#: publication and no date — see :func:`_settle_status`, which keeps that true.
_NOT_GOING_OUT = (ContentStatus.DRAFT, ContentStatus.REVIEW, ContentStatus.ARCHIVED)


def _settle_status(db: Session, content: Content, previous: ContentStatus) -> None:
    """Make the queue and the rows that went out agree with a hand-set status.

    Called by every door that lets a caller *name* a status — the PATCH, the
    ``/status`` endpoint, the approve button and its bulk form — after the
    column is written and before the commit. Two rules, each of which one of
    those doors used to apply and the others did not:

    **A piece that is not going out has nothing armed.** Archiving cancelled
    the queue (:func:`app.services.publishing_service.cancel_armed`) because
    the review queue's Reject once published the thing it had just rejected.
    Moving a piece back to ``draft`` or ``review`` is the same statement made
    more gently — "not this, not yet" — and it left the queue exactly as it
    was. ``due_publications`` selects on the row's status and time alone, and
    ``execute``'s last gate asks only whether the piece is archived, so a piece
    pulled back to draft on Monday to be reworked went out on Tuesday, with
    whatever the body said by then. The status column read ``draft`` right up
    until the worker's ``sync_content_status`` flipped it to ``published``.

    **A piece that went out is published or archived, and nothing else.**
    ``PATCH {"status": "draft"}`` on a published piece is a 409 — it is still
    live everywhere it went. But the check read the column, and archiving
    moves the column, so the refusal lasted one call:

        PATCH {"status": "archived"}   200 — the allowed move
        PATCH {"status": "draft"}      200 — and the piece is off the RSS feed,
                                       out of the published count, invisible
                                       to the headline sweep, while its post
                                       is up on Dev.to

    The same shape as the edit freeze that :func:`_is_live` fixed, and the
    same answer: ask the rows. Un-archiving a piece with a live publication
    puts it back where the rows say it is, which is ``published`` — the one
    status a caller cannot name by hand (see ``_settable_status``), so it is
    restored here rather than requested. The caller asked for the piece back
    in circulation, and this is the circulation it is in.

    Does not commit. Idempotent: running it twice over the same piece changes
    nothing the first run did not.
    """
    if (
        previous == ContentStatus.ARCHIVED
        and content.status != ContentStatus.ARCHIVED
        and content.went_out
    ):
        # The derivation's own first arm: ``published``, and ``published_at``
        # stamped if the piece somehow never was.
        publishing_service.sync_content_status(content)
    if content.status in _NOT_GOING_OUT:
        # Whether this call moved the piece there or it was there already: in
        # both cases the queue must agree with the column.
        publishing_service.cancel_armed(db, content)


def _assert_review_ready(
    db: Session, content: Content, previous_status: ContentStatus
) -> None:
    """Refuse a DRAFT→REVIEW move for a piece nobody should have to read.

    The review queue is a request for somebody's attention. Everything else in
    Herald that puts a piece there has already earned it — the autopilot writes
    a piece, runs it through five gates, and routes it to review *because* one
    of them fired, so the reviewer opens it knowing why. A draft promoted by
    hand carried no such claim, and the two things a thin generation produces —
    a body that is four fenced blocks and a sentence, or one that reads like a
    standards document — both went into the same queue looking exactly like a
    piece worth reading.

    So the floor is applied here and nowhere else. Not on approve: approving is
    a human saying yes, and a gate that overrules that is a gate that has
    decided it knows better than the reviewer. Not on archive, which is somebody
    saying the piece is not going out — refusing *that* for low quality would
    trap the worst pieces in the queue. And not on the autopilot's own route to
    review, which has nowhere left to send a piece it refuses.

    Scored from the piece as it *will be*, which is why this runs after the
    PATCH has applied its fields: a request that fixes the body and submits it
    in one call is a request that should pass. Nothing is committed on the way
    out — the rollback is explicit rather than left to the session closing,
    because a half-applied edit surviving this refusal would be the one outcome
    worse than either answer.
    """
    if not settings.content_quality_gate_enabled:
        return
    if previous_status != ContentStatus.DRAFT or content.status != ContentStatus.REVIEW:
        return

    report = quality.report_for(content)
    floor = settings.content_quality_min_score
    if report.score >= floor:
        return

    db.rollback()
    # Every component, not just the verdict. "48, and the floor is 50" tells a
    # writer they have been refused; "93% of the body is code" tells them what
    # to do about it, and the numbers are the same ones the editor is already
    # showing in the `quality` block of this piece.
    detail = (
        f"This piece scores {report.score} out of 100 and the review queue "
        f"asks for at least {floor}. SEO {report.seo_score}"
    )
    if report.readability.reading_ease is not None:
        detail += (
            f", reading ease {report.readability.reading_ease} "
            f"(grade {report.readability.grade_level})"
        )
    detail += (
        f", {round(report.code_ratio * 100)}% of the body is code. "
        "Edit it and submit again."
    )
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _owned_content_map(
    content_ids: Sequence[int], db: Session, user: User
) -> dict[int, Content]:
    """Every requested piece this user owns, keyed by id, in one query.

    The ``/bulk/*`` endpoints are the review queue's batch buttons, so the id
    list is as long as the user's selection — up to a hundred, with no
    pagination ceiling to hide behind. Fetching one at a time and then reaching
    through ``content.project`` for the ownership check costs two round trips
    per item; a 20-item batch spent 40 queries where one does.

    Ownership is enforced in the WHERE clause rather than after the load: a
    piece belonging to someone else is simply absent from the map, which is the
    same thing the caller reports for an id that does not exist. ``project``
    rides along because every caller renders or reasons about it.
    """
    if not content_ids:
        return {}
    stmt = (
        select(Content)
        .options(joinedload(Content.project))
        .join(Project, Project.id == Content.project_id)
        .where(Content.id.in_(set(content_ids)), Project.user_id == user.id)
    )
    return {content.id: content for content in db.scalars(stmt).unique()}


def _to_out(content: Content) -> ContentOut:
    """A listing row. Reads no article text — see the ``defer`` on its callers.

    ``word_count`` and ``read_minutes`` both come off the stored count now, so
    the only field here that would touch ``body_markdown`` is one nobody added.
    The paged callers below defer that column so a listing of 500 pieces stops
    shipping 500 article bodies to render 500 word counts; ``_to_detail``, which
    genuinely wants the text, does not defer it.
    """
    return ContentOut(
        **{
            key: getattr(content, key)
            for key in (
                "id", "project_id", "content_type", "status", "title", "slug",
                "excerpt", "meta_description", "keywords", "tags", "canonical_url",
                "cover_image_url", "focus_keyword", "version",
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
        # The same report the review gate applies, from the same call. A score
        # the editor cannot see until it refuses a transition is a rule the
        # writer has to discover by hitting it.
        quality=QualityOut(**quality.report_for(content).as_dict()),
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


@router.get(
    "",
    response_model=list[ContentOut],
    summary="Your content, filtered and paged",
    responses=errors(*AUTHENTICATED),
)
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
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ContentOut]:
    """Every piece this account owns, newest first.

    The total before paging is in ``X-Total-Count``. Unlike the narrowed
    listings elsewhere, an unknown ``project_id`` filters to nothing rather
    than 404ing — this is a filter over your own rows, not a lookup.
    """
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
    # ``Content.id`` is the tiebreaker, and it is not decoration: ``created_at``
    # is not unique, and an ``OFFSET`` walk over a sort the database is free to
    # break either way is a walk that can hand back one row twice and never
    # show another. See ``tests/test_paging_is_a_total_order.py``.
    query = (
        base
        .options(joinedload(Content.project), defer(Content.body_markdown))
        .order_by(Content.created_at.desc(), Content.id.desc())
        .offset(offset)
        .limit(limit)
    )

    return [_to_out(c) for c in db.scalars(query)]


# NB: the literal paths under /content (``/queue/...``) are declared here,
# *before* ``GET /{content_id}``. FastAPI matches in declaration order, so a
# ``/{content_id}`` registered first would swallow ``/queue/review`` and 422 on
# "queue" not being an int.


@router.get(
    "/queue/review",
    response_model=list[ContentOut],
    summary="Drafts waiting on a human",
    responses=errors(*AUTHENTICATED),
)
def review_queue(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: ListOffset = 0,
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
        base.options(joinedload(Content.project), defer(Content.body_markdown))
        # Tiebroken by id, for the reason ``list_content`` gives: the review
        # queue is the listing most likely to hold a batch the autopilot wrote
        # in one transaction, and on Postgres those share a ``created_at``.
        .order_by(Content.created_at.desc(), Content.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_to_out(c) for c in rows]


@router.get(
    "/queue/publications",
    response_model=list[PublicationOut],
    summary="Publications in flight",
    responses=errors(*AUTHENTICATED),
)
def publication_queue(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: ListOffset = 0,
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
        # The worst of the four for ties, which is why it is spelled out here:
        # every ``pending``, ``publishing`` and ``failed`` row has a null
        # ``scheduled_for`` and so sorts equal to every other one, and arming a
        # piece for three platforms at one time gives three more rows with the
        # same instant on them. Without ``Publication.id`` the queue's paging
        # was over a sort where most of the rows were interchangeable.
        base.order_by(
            Publication.scheduled_for.is_(None).desc(),
            Publication.scheduled_for,
            Publication.id,
        )
        .offset(offset)
        .limit(limit)
    )
    return [PublicationOut.model_validate(p) for p in rows]


@router.post(
    "/bulk/approve",
    response_model=BulkResultOut,
    summary="Approve many pieces at once",
    # No 404 and no 409: a piece that is missing, someone else's, or already
    # published comes back in `failed` with a reason. The batch answers 200
    # whatever the mix, because a partial success is the normal outcome.
    responses=errors(*AUTHENTICATED),
)
def bulk_approve_content(
    payload: BulkContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Approve many review-queue pieces in one call, skipping any that can't be.

    Each approved piece is released exactly as the single-item endpoint releases
    it — the two must not disagree about what Approve means, and the batch is
    the button the review queue actually offers.

    With ``dry_run`` the same verdicts come back and nothing is written or
    released. Worth it here despite approving being reversible, because
    releasing is not: a project on ``auto`` publishes what this approves, so
    "which of these forty would go straight out?" is a question with a real
    answer and no way to ask it afterwards.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    approved: list[int] = []
    owned = _owned_content_map(payload.content_ids, db, user)
    for content_id in payload.content_ids:
        content = owned.get(content_id)
        if content is None:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        if content.status == ContentStatus.PUBLISHED:
            failed.append(
                BulkFailureOut(content_id=content_id, reason="Already published")
            )
            continue
        succeeded.append(content_id)
        if payload.dry_run:
            continue
        previous_status = content.status
        content.status = ContentStatus.APPROVED
        _settle_status(db, content, previous_status)
        approved.append(content_id)
    if payload.dry_run:
        # Nothing above wrote, but the loop still loaded rows and something
        # earlier in the request may have touched one. Rolling back rather than
        # simply not committing is what makes "changes nothing" a property of
        # this endpoint instead of a property of how carefully the loop was
        # read: the session goes back to the connection pool either way, and
        # ``get_db`` does not roll back a clean-looking session for us.
        db.rollback()
        return BulkResultOut.of(succeeded, failed, dry_run=True)
    db.commit()
    # After the commit, so a release that dispatches a worker cannot hand it a
    # row this request has not written yet — which means re-reading the rows,
    # since the commit expired every one of them. ``release_approved`` walks
    # ``publications``, ``project`` and ``project.user`` before deciding, so
    # loading them here is the difference between one query for the batch and
    # three per piece. Same eager set as the backstop sweep in
    # :func:`app.tasks.publish_tasks.release_approved_content`.
    if approved:
        for content in db.scalars(
            select(Content)
            .options(
                joinedload(Content.project).joinedload(Project.user),
                selectinload(Content.publications),
            )
            .where(Content.id.in_(approved))
        ).unique():
            content_pipeline.release_approved(db, content)
    return BulkResultOut.of(succeeded, failed)


@router.post(
    "/bulk/reject",
    response_model=BulkResultOut,
    summary="Archive many pieces at once",
    responses=errors(*AUTHENTICATED),
)
def bulk_reject_content(
    payload: BulkContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Archive many review-queue pieces in one call.

    There is no ``rejected`` status — archiving is the same "not going out"
    outcome a human hits one at a time from the editor, just batched.

    ``dry_run`` reports the same verdicts and archives nothing, which is the
    one of these four previews with an obvious use: a selection made from a
    filtered list is easy to get wrong by one page, and this is the batch that
    cancels armed publications on its way through.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    owned = _owned_content_map(payload.content_ids, db, user)
    for content_id in payload.content_ids:
        content = owned.get(content_id)
        if content is None:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        if content.status == ContentStatus.PUBLISHED:
            failed.append(
                BulkFailureOut(content_id=content_id, reason="Already published")
            )
            continue
        succeeded.append(content_id)
        if payload.dry_run:
            continue
        content.status = ContentStatus.ARCHIVED
        # Rejecting a piece has to take it off the queue as well as out of the
        # list, or the beat sweep publishes the thing that was just rejected —
        # see :func:`app.services.publishing_service.cancel_armed`, which clears
        # the piece's own calendar date along with the publications.
        publishing_service.cancel_armed(db, content)
    if payload.dry_run:
        db.rollback()
        return BulkResultOut.of(succeeded, failed, dry_run=True)
    db.commit()
    return BulkResultOut.of(succeeded, failed)


@router.post(
    "/bulk/publish",
    response_model=BulkResultOut,
    summary="Queue many pieces for the same platforms",
    # As on `/bulk/approve`: everything the single-item publish would raise as
    # a 400, 409 or 422 is recorded against the one piece it applies to.
    responses=errors(*AUTHENTICATED),
)
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

    ``dry_run`` runs those checks and queues nothing. This is the batch that
    most needs it: the failures here are the ones a caller cannot predict from
    the list they are looking at — a platform they have not connected, an
    adapter that is not finished, a link that has rotted since the piece was
    written — and half a batch queued is half a batch that has to be found and
    unqueued one publication at a time.

    ``timezone`` applies to the whole batch, as ``scheduled_for`` does. A batch
    is one instant for every piece in it; staggering across a week is what the
    calendar's reschedule and ``POST /content/{id}/schedule`` are for.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    single = PublishRequestIn(
        platforms=payload.platforms,
        scheduled_for=payload.scheduled_for,
        timezone=payload.timezone,
        as_draft=payload.as_draft,
        allow_broken_links=payload.allow_broken_links,
    )
    owned = _owned_content_map(payload.content_ids, db, user)
    for content_id in payload.content_ids:
        content = owned.get(content_id)
        if content is None:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        try:
            if payload.dry_run:
                _assert_publishable(content, single, user)
            else:
                _queue_publish(content, single, db, user)
        except _PublishError as exc:
            failed.append(BulkFailureOut(content_id=content_id, reason=exc.detail))
            continue
        succeeded.append(content_id)
    if payload.dry_run:
        db.rollback()
        return BulkResultOut.of(succeeded, failed, dry_run=True)
    return BulkResultOut.of(succeeded, failed)


@router.post(
    "/bulk/retry",
    response_model=BulkResultOut,
    summary="Retry every stopped publication on many pieces",
    responses=errors(*AUTHENTICATED),
)
def bulk_retry_content(
    payload: BulkContentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkResultOut:
    """Re-arm the failed publications on many pieces in one call.

    The batch form of ``POST /content/{id}/retry``, and it re-arms through the
    same :func:`_rearm`, so a syndicated copy is held behind its original here
    exactly as it is there.

    A piece with nothing retryable on it is a *failure* in the result rather
    than a silent success. The distinction matters on the publications list,
    where "Retry all" over a mixed selection is the button this serves: a
    caller told 200-and-succeeded for a piece whose only publication is live
    has been told its retry happened.

    ``dry_run`` answers the same verdicts and re-arms nothing — including the
    dispatch, which is the part that cannot be taken back: a retry that has
    reached a worker is a request already on its way to a platform.
    """
    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []
    to_dispatch: list[int] = []
    owned = _owned_content_map(payload.content_ids, db, user)

    for content_id in payload.content_ids:
        content = owned.get(content_id)
        if content is None:
            failed.append(BulkFailureOut(content_id=content_id, reason="Not found"))
            continue
        # The same refusal the single-piece endpoint makes, for the same
        # reason: arming an archived piece cannot publish it — `execute`
        # cancels the row at the last gate — so it would answer success to a
        # caller whose piece is not going anywhere.
        if content.status == ContentStatus.ARCHIVED:
            failed.append(
                BulkFailureOut(
                    content_id=content_id,
                    reason="Archived — take it out of the archive first.",
                )
            )
            continue
        retryable = [p for p in content.publications if p.status in RETRYABLE]
        if not retryable:
            failed.append(
                BulkFailureOut(content_id=content_id, reason="Nothing to retry")
            )
            continue
        succeeded.append(content_id)
        if payload.dry_run:
            continue
        for publication in retryable:
            if _rearm(content, publication):
                to_dispatch.append(publication.id)

    if payload.dry_run:
        db.rollback()
        return BulkResultOut.of(succeeded, failed, dry_run=True)

    # One commit for the batch, then one dispatch. Committing per piece would
    # leave a half-retried batch behind if a later row raised, and dispatching
    # before the commit would hand a worker rows this request has not written.
    db.commit()
    _dispatch(to_dispatch)
    return BulkResultOut.of(succeeded, failed)


@router.post(
    "/bulk/archive-old",
    response_model=ArchiveOldOut,
    summary="Archive pieces older than a given age",
    responses=errors(*AUTHENTICATED),
)
def archive_old_content(
    payload: ArchiveOldIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ArchiveOldOut:
    """Archive this account's old drafts, for clearing out a review queue.

    The only write in this router that acts on rows the caller has not named
    one by one, which is what ``dry_run`` is for and why ``older_than_days``
    has no default.

    **Published pieces are never swept**, whatever ``statuses`` asks for. The
    rest of the tree treats archiving a live post as a deliberate act with a
    consequence — it cancels the armed rows and hides the piece from the lists
    — and doing that to a month of live posts because somebody typed 30 is not
    a bulk convenience, it is an outage in the analytics. The single-piece
    PATCH remains the way to archive something that is live.

    Measured on ``created_at`` rather than ``updated_at``: the question is "how
    long has this been sitting here", and a draft nudged last week has still
    been sitting here since it was written.
    """
    statuses = set(payload.statuses or _ARCHIVE_SWEEP_DEFAULT_STATUSES)
    statuses -= {ContentStatus.PUBLISHED, ContentStatus.ARCHIVED}
    if not statuses:
        # Everything asked for was filtered out. An empty IN would match no
        # rows and answer a cheerful zero, which reads as "nothing was old
        # enough" rather than "nothing you asked for can be swept this way".
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing sweepable in that status list — published and "
            "archived pieces are not swept by age.",
        )

    cutoff = utcnow() - timedelta(days=payload.older_than_days)
    query = (
        select(Content)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Content.status.in_(statuses),
            Content.created_at < cutoff,
        )
        .order_by(Content.id)
    )
    if payload.project_id is not None:
        query = query.where(Content.project_id == payload.project_id)

    candidates = list(db.scalars(query))
    if payload.dry_run:
        return ArchiveOldOut(
            archived=[c.id for c in candidates], count=len(candidates), dry_run=True
        )

    for content in candidates:
        content.status = ContentStatus.ARCHIVED
        # Archiving has to take the piece off the queue as well as out of the
        # list, or the beat sweep publishes what was just archived — the same
        # pairing ``/bulk/reject`` and the PATCH both make. An old *approved*
        # draft is exactly the case with something armed behind it.
        publishing_service.cancel_armed(db, content)
    db.commit()
    return ArchiveOldOut(
        archived=[c.id for c in candidates], count=len(candidates), dry_run=False
    )


@router.get(
    "/preview/{token}",
    response_model=PublicPreviewOut,
    summary="A shared draft, no token required",
    responses={
        **errors(status.HTTP_429_TOO_MANY_REQUESTS),
        # Not the catalogue's 404: there is no owner in this exchange to
        # distinguish from, and the interesting cases are the ones the shared
        # wording does not cover.
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorOut,
            "description": (
                "No such link — or one that has been revoked or has expired. "
                "The three are one answer on purpose."
            ),
        },
    },
)
@limiter.limit(settings.rate_limit_public_read)
def get_public_preview(
    token: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> PublicPreviewOut:
    """A reviewer's view of one draft, no bearer token required.

    Declared ahead of ``/{content_id}`` so a token here is never swallowed by
    that route's int-typed path param — see the module docstring notes on
    ``/queue/*`` and ``/bulk/*`` above for the same reason. Rate-limited like
    every other anonymous read in the API: a token is unguessable, but "no
    token required" and "free to hammer" are different claims.
    """
    content = preview_links.resolve(db, token)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
    return PublicPreviewOut(
        title=content.title,
        body_markdown=content.body_markdown,
        excerpt=content.excerpt,
        cover_image_url=content.cover_image_url,
        word_count=content.word_count,
        read_minutes=content.read_minutes,
        project_name=content.project.name if content.project else None,
    )


@router.get(
    "/{content_id}/internal-links",
    response_model=list[InternalLinkSuggestionOut],
    summary="Other posts worth linking to",
    responses=errors(*OWNED),
)
def internal_link_suggestions(
    content_id: RowId,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[InternalLinkSuggestionOut]:
    """Other published posts in this project worth linking to, by keyword overlap."""
    content = _owned_content(content_id, db, user)
    # Six columns, not entities. The candidate set is every published piece in
    # the project — unbounded by anything except how long the project has been
    # running — and ``suggest_internal_links`` reads an id, a title, a slug, two
    # keyword fields and a URL off each one. A ``Content`` row to reach them is
    # the whole article body and four JSON columns, and, through
    # ``lazy="selectin"``, a second SELECT over every publication of every one
    # of those pieces. A project with a hundred published posts on three
    # platforms answered "what could I link to?" with a hundred article bodies
    # and three hundred publication rows.
    candidates = db.execute(
        select(
            Content.id,
            Content.title,
            Content.slug,
            Content.keywords,
            Content.focus_keyword,
            Content.canonical_url,
        ).where(
            Content.project_id == content.project_id,
            Content.id != content.id,
            Content.status == ContentStatus.PUBLISHED,
        )
    ).all()
    suggestions = seo.suggest_internal_links(
        keywords=content.keywords,
        focus_keyword=content.focus_keyword,
        candidates=[
            {
                "content_id": row.id,
                "title": row.title,
                "slug": row.slug,
                "keywords": row.keywords,
                "focus_keyword": row.focus_keyword,
                "canonical_url": row.canonical_url,
            }
            for row in candidates
        ],
        limit=limit,
    )
    return [InternalLinkSuggestionOut(**s) for s in suggestions]


@router.get(
    "/{content_id}/metrics",
    response_model=ContentEngagementOut,
    summary="How this piece performed, everywhere it went",
    responses=errors(*OWNED),
)
def content_engagement_metrics(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentEngagementOut:
    """The newest reading per platform, the totals, and the trend behind them.

    The read Herald was missing: every other metrics endpoint is organised by
    *publication*, and "how did this post do" is a question about a piece —
    which is three publications on three platforms reporting three different
    sets of fields. See :mod:`app.services.content_engagement`, including why a
    total carries the list of platforms that reported it and why a counter
    nobody reported stays ``null`` rather than becoming zero.

    Reads what the poller has already stored; it does not go and ask the
    platforms. Collection is :mod:`app.tasks.metrics_tasks`' job and runs on its
    own schedule — a page load that fanned out to Dev.to and Bluesky would be
    slow, rate-limited, and different on every refresh.
    """
    content = _owned_content(content_id, db, user)
    return ContentEngagementOut(
        **content_engagement.for_content(db, content, user_id=user.id).as_dict()
    )


@router.get(
    "/{content_id}/platform-check",
    response_model=PlatformChecksOut,
    summary="Whether this piece fits everywhere it is going",
    responses=errors(*OWNED, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def platform_checks(
    content_id: RowId,
    platforms: str = Query(
        default="",
        description=(
            "Comma-separated platforms to check. Defaults to where this piece "
            "is actually going: the publications it already has, then the "
            "project's autopilot destinations."
        ),
    ),
    as_draft: bool = Query(
        default=False,
        description=(
            "Check the draft-publish path instead. Changes the answer where a "
            "platform has no draft state — Bluesky refuses one outright."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlatformChecksOut:
    """What each destination will make of this piece, without contacting any of them.

    Answers the two questions the publish path could only answer by trying:
    will this be refused, and will it arrive intact. See
    :mod:`app.services.platform_check` — including why this reports rather than
    refuses, and why the "no live connection" sentence here is the same string
    the failed row would have carried.

    GET and free. Every check reads the piece, the adapters' own constants and
    the connections' *status*; nothing here decrypts a credential or touches a
    platform, so it is safe on a draft and cheap enough for a preview panel.
    """
    content = _owned_content(content_id, db, user)

    if platforms.strip():
        try:
            wanted = [
                Platform(name.strip().lower())
                for name in platforms.split(",")
                if name.strip()
            ]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown platform: {exc}",
            ) from exc
    else:
        wanted = platform_check.destinations_for(content)

    verdicts = platform_check.check(
        db, content, wanted, user_id=user.id, as_draft=as_draft
    )
    return PlatformChecksOut(
        content_id=content.id,
        # A piece with nowhere to go is not publishable, and answering True for
        # it would put a green tick on the one case that most needs a red one.
        publishable=bool(verdicts) and all(v.publishable for v in verdicts),
        platforms=[
            PlatformCheckOut(
                platform=v.platform,
                publishable=v.publishable,
                findings=[PreflightFindingOut(**f.as_dict()) for f in v.findings],
            )
            for v in verdicts
        ],
    )


@router.get(
    "/{content_id}/social",
    response_model=SocialCardsOut,
    summary="How this piece unfurls in a feed",
    responses=errors(*OWNED),
)
def social_cards_preview(
    content_id: RowId,
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


@router.post(
    "/{content_id}/repurpose",
    response_model=RepurposeOut,
    summary="Draft social copy from a piece",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_ai_assist, key_func=account_key)
def repurpose_content(
    content_id: RowId,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RepurposeOut:
    """Draft a Twitter thread and a LinkedIn post from this piece.

    POST, not GET, and runs inline like ``/generate``: this costs an LLM call
    and nothing is persisted, so the caller gets a fresh set of snippets every
    time — there is no cached result to invalidate. Which is also why it is
    rate-limited per account: nothing is cached, so every call is a fresh call
    against a shared free-tier quota.
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


@router.post(
    "/{content_id}/edit",
    response_model=InlineEditOut,
    summary="Rewrite one passage of a draft",
    responses=errors(
        *OWNED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
@limiter.limit(settings.rate_limit_ai_assist, key_func=account_key)
def edit_passage(
    content_id: RowId,
    payload: InlineEditIn,
    request: Request,
    response: Response,
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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


@router.post(
    "/{content_id}/headlines",
    response_model=HeadlineVariantsOut,
    summary="Draft alternative headlines",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_ai_assist, key_func=account_key)
def generate_headline_variants(
    content_id: RowId,
    request: Request,
    response: Response,
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


@router.post(
    "/{content_id}/headlines/apply",
    response_model=ContentOut,
    summary="Swap in a headline",
    responses=errors(*OWNED),
)
def apply_content_headline(
    content_id: RowId,
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
    _dispatch_headline_sync(content.id)
    return _to_out(content)


@router.get(
    "/{content_id}/headlines/performance",
    response_model=list[HeadlineWindowOut],
    summary="What each headline earned while live",
    responses=errors(*OWNED),
)
def content_headline_performance(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[HeadlineWindowOut]:
    """What each headline (including the current one) earned while it was live."""
    content = _owned_content(content_id, db, user)
    return [
        HeadlineWindowOut(**w.as_dict()) for w in headlines.performance(content, db)
    ]


def _dispatch_headline_sync(content_id: int) -> None:
    """Tell the live destinations the headline changed.

    Off the request thread when there is a broker, because it is up to four
    platform calls and the swap it follows is already committed and already
    true. Inline when there is not — a single-process install must not silently
    leave every live post showing the old headline, which is a divergence
    nothing later reconciles. Mirrors ``_dispatch_publications`` and
    ``webhooks.dispatch``.
    """
    from app.tasks import headline_tasks

    if settings.celery_enabled:
        try:
            headline_tasks.sync_headline.delay(content_id)
            return
        except Exception as exc:
            logger.warning("headline sync dispatch failed, running inline: %s", exc)
    try:
        headline_tasks.sync_headline(content_id)
    except Exception:
        # ``sync_title`` records every failure as an outcome and is documented
        # not to raise, so reaching here is something outside it. The headline
        # is swapped either way; ``GET /headlines/reach`` is where the user
        # finds out a destination did not get it.
        logger.exception("headline sync failed inline for content %s", content_id)


@router.get(
    "/{content_id}/headlines/reach",
    response_model=HeadlineReachOut,
    summary="Which destinations show the current headline",
    responses=errors(*OWNED),
)
def content_headline_reach(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HeadlineReachOut:
    """Which live destinations are showing this piece's current headline.

    Read this beside ``/headlines/winner``. A contest that reports "not enough
    data" while the piece is doing well everywhere is usually not a shortage of
    engagement — it is that none of the destinations carrying the piece can be
    retitled, so none of their engagement is evidence about the headline.
    """
    content = _owned_content(content_id, db, user)
    unreachable = headlines.unreachable(content, db)
    tracking = len(headlines.tracking_publications(content, db))
    return HeadlineReachOut(
        tracking=tracking,
        live=tracking + len(unreachable),
        unreachable=[HeadlineUnreachableOut(**row) for row in unreachable],
    )


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


@router.get(
    "/{content_id}/headlines/winner",
    response_model=HeadlineWinnerOut,
    summary="Which headline is winning",
    responses=errors(*OWNED),
)
def content_headline_winner(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HeadlineWinnerOut:
    """Which headline is winning, and whether the lead is worth acting on.

    Read-only: nothing is applied here, so the answer can be shown next to the
    headline without committing to it.
    """
    content = _owned_content(content_id, db, user)
    return _winner_out(headlines.pick_winner(headlines.performance(content, db)))


@router.post(
    "/{content_id}/headlines/auto-select",
    response_model=HeadlineWinnerOut,
    summary="Adopt the winning headline",
    responses=errors(*OWNED),
)
def apply_headline_winner(
    content_id: RowId,
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
        _dispatch_headline_sync(content.id)
    return _winner_out(verdict, applied=applied)


@router.get(
    "/{content_id}",
    response_model=ContentDetail,
    summary="One piece in full",
    responses=errors(*OWNED),
)
def get_content(
    content_id: RowId,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """One piece with its body, its SEO issues and its format problems.

    The listing endpoints return the summary shape; this is the detail shape,
    and the difference is that it carries ``body_markdown``.

    The ``ETag`` header carries the piece's current version. Send it back as
    ``If-Match`` on the PATCH and an edit that would overwrite somebody else's
    is refused — see :func:`update_content`. It is *not* a cache validator:
    ``Cache-Control: no-store`` is set on every response in this API, and this
    endpoint has no ``If-None-Match`` branch to go with it.
    """
    content = _owned_content(content_id, db, user)
    response.headers["ETag"] = _etag(content)
    return _to_detail(content)


@router.get(
    "/{content_id}/links",
    response_model=LinkCheckOut,
    summary="Check every link in the body",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_link_check, key_func=account_key)
def check_links(
    content_id: RowId,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LinkCheckOut:
    """HEAD-check every URL in the body, on demand.

    Not folded into ``GET /{content_id}``: that endpoint is loaded every time the
    editor opens and this one makes up to ``link_check_max_urls`` outbound
    requests. Kept separate so reading a draft stays free.

    Rate-limited per account for the same reason it is separate. One call is up
    to ``link_check_max_urls`` outbound requests, from Herald's address, to
    hosts named in a document the caller wrote — which is an amplifier if it can
    be replayed. The URLs themselves are already vetted against private and
    loopback addresses on every hop (:mod:`app.services.link_check`); this caps
    the volume rather than the destination.
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


def _to_preview_link(link: PreviewLink, *, url: str | None = None) -> PreviewLinkOut:
    return PreviewLinkOut(
        id=link.id,
        url=url,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        view_count=link.view_count,
        last_viewed_at=link.last_viewed_at,
        created_at=link.created_at,
    )


@router.get(
    "/{content_id}/preview-links",
    response_model=list[PreviewLinkOut],
    summary="Share links issued for this draft",
    responses=errors(*OWNED),
)
def list_preview_links(
    content_id: RowId,
    response: Response,
    # The one listing here that had no ceiling at all. Nothing prunes preview
    # links and revoking keeps the row, so a draft that goes round a team for a
    # few months accumulates them without limit — see
    # ``preview_links.list_for_content``. Same bounds and the same
    # ``X-Total-Count`` as every other listing in this router.
    limit: int = Query(default=50, ge=1, le=200),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PreviewLinkOut]:
    """The links issued for this draft, newest first.

    The URL only ever appears once, at creation — a listing can show that a
    link exists and let it be revoked, not what it is. The total before paging
    is in ``X-Total-Count``.
    """
    content = _owned_content(content_id, db, user)
    response.headers["X-Total-Count"] = str(
        preview_links.count_for_content(db, content.id)
    )
    return [
        _to_preview_link(link)
        for link in preview_links.list_for_content(
            db, content.id, limit=limit, offset=offset
        )
    ]


@router.post(
    "/{content_id}/preview-links",
    response_model=PreviewLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a share link",
    responses=errors(*OWNED),
)
def create_preview_link(
    content_id: RowId,
    payload: PreviewLinkCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PreviewLinkOut:
    """Mint a link that shows this draft to somebody without an account.

    The only response that ever carries ``url``: what is stored is a hash, so
    a link that is not written down here cannot be recovered — only revoked
    and reissued.
    """
    content = _owned_content(content_id, db, user)
    link, raw_token = preview_links.issue(db, content, ttl_hours=payload.ttl_hours)
    return _to_preview_link(link, url=preview_links.preview_url(raw_token))


@router.delete(
    "/{content_id}/preview-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a share link",
    responses=errors(*OWNED),
)
def revoke_preview_link(
    content_id: RowId,
    link_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Stop a link working. The row stays, so the view count survives it.

    404 covers both halves of the path: no such piece, and no such link on
    this piece.
    """
    content = _owned_content(content_id, db, user)
    link = next(
        (row for row in preview_links.list_for_content(db, content.id) if row.id == link_id),
        None,
    )
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
    preview_links.revoke(db, link)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #


@router.post(
    "/generate",
    response_model=ContentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a piece with the AI engine",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_ai_generate, key_func=account_key)
def generate_content(
    payload: GenerateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Draft a piece with the AI engine.

    Runs inline rather than on a worker: the user is watching, and a draft that
    arrives in the response is worth the ~20s wait. The autopilot path is the
    one that goes through Celery.

    The most expensive thing an authenticated caller can ask for, and so the
    tightest of the per-account budgets — the daily quotas it spends are shared
    by every account on the install.
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


@router.post(
    "",
    response_model=ContentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Write a piece by hand",
    responses=errors(*OWNED),
)
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


@router.patch(
    "/{content_id}",
    response_model=ContentDetail,
    summary="Edit a piece",
    responses=errors(
        *OWNED,
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_409_CONFLICT,
        status.HTTP_412_PRECONDITION_FAILED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
def update_content(
    content_id: RowId,
    payload: ContentUpdate,
    response: Response,
    if_match: str | None = Header(
        default=None,
        alias="If-Match",
        description=(
            'The ``version`` of the piece you loaded, as an entity tag — '
            '``If-Match: "4"``. The edit is refused with a 412 if anyone has '
            "written the piece since. Omit it to save unconditionally."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Patch a piece. Omitted fields are left alone.

    A published piece is nearly frozen: the only edit it accepts is archiving,
    because everything else here would disagree with what is live on the
    platforms. Both refusals are 409s that say which one you hit.

    Setting ``status`` to ``approved`` releases the piece exactly as the
    Approve button does, so a scripted caller does not need to know about a
    second endpoint. Setting it to ``review`` from ``draft`` is the one
    transition with a quality floor on it — see :func:`_assert_review_ready`,
    which scores the piece *after* this call's edits, so fixing a body and
    submitting it in the same request is one request.

    ``scheduled_for`` means here what it means everywhere else: a time in the
    past or beyond the horizon is refused with a 422, and the piece's publications
    move with it — the ones still waiting, not the ones already out.

    Send ``If-Match`` with the ``version`` from the copy you are editing and a
    save that would land on top of somebody else's is refused with a 412. The
    new version comes back in the ``ETag`` header and in the body, so the next
    save can carry it. See :attr:`app.models.content.Content.version`.
    """
    content = _owned_content(content_id, db, user)
    # Before anything else reads the payload: a caller holding a stale copy is
    # told so whether or not the fields it is sending would also have been
    # refused for some other reason. The freeze below is about this piece's
    # state, the precondition is about *which* piece the caller thinks it has,
    # and answering the second question first is what makes "reload and try
    # again" the right advice for every 412 this endpoint sends.
    _assert_if_match(content, if_match)

    data = payload.model_dump(exclude_unset=True)
    reject_nulls(Content, data)

    if "scheduled_for" in data:
        # The same rule every other writer of this column applies to it, for the
        # same reason. ``_queue_publish`` and the calendar's reschedule both run
        # the requested time through :func:`app.services.scheduling.normalize`,
        # which refuses a time in the past — "a mistyped year currently means
        # publish immediately, which is the one outcome somebody setting a date
        # did not want". This endpoint writes the identical column and applied
        # nothing, so ``PATCH {"scheduled_for": "2025-..."}`` was accepted with a
        # 200 where ``POST /schedule`` answers 422 for the same value, and
        # ``release_approved`` then queued the piece for now.
        try:
            data["scheduled_for"] = scheduling.normalize(data["scheduled_for"])
        except scheduling.ScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

    if _is_live(content) and set(data) - {"status", "scheduled_for"}:
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

    # Read before anything is written, because the gate below asks what the
    # piece is moving *from* and the assignment loop is about to overwrite it.
    previous_status = content.status

    # Bank the text that is about to be replaced, before the assignment loop
    # replaces it. Here rather than after, because a snapshot taken afterwards
    # captures the new text under the old version number — the one arrangement
    # that makes a history actively misleading.
    #
    # Only when something actually changes: the editor autosaves on a timer, so
    # most PATCHes re-send a body identical to the stored one, and a history
    # where nine entries in ten say "no changes" is a log with the useful rows
    # hidden in it. `snapshot_if_changing` also decides *what* changed and
    # writes the field names into the note, which is what the sidebar shows.
    #
    # Not committed here. The snapshot joins this request's transaction, so a
    # request that then fails `_assert_review_ready` leaves no history entry for
    # an edit that never happened.
    revisions.snapshot_if_changing(db, content, data, author_user_id=user.id)

    if "title" in data and data["title"] != content.title:
        content.slug = unique_content_slug(db, content.project_id, data["title"])
    if "keywords" in data and data["keywords"] is not None:
        data["keywords"] = seo.normalize_keywords(data["keywords"])
        # Keep focus_keyword in sync unless the caller set it explicitly.
        if "focus_keyword" not in data and data["keywords"]:
            data["focus_keyword"] = data["keywords"][0]

    for key, value in data.items():
        setattr(content, key, value)

    # After the fields are applied and before anything is committed: a request
    # that rewrites the body and submits it for review in one call is judged on
    # the body it is sending, not on the one it is replacing.
    _assert_review_ready(db, content, previous_status)

    if "scheduled_for" in data:
        # And the piece's date has to reach the rows that act on it. Every other
        # writer of ``content.scheduled_for`` moves the publications with it —
        # ``_queue_publish`` arms them at the new time, the calendar's reschedule
        # re-times them, ``unschedule_content`` and ``cancel_armed`` clear both
        # together. This one set the column alone, so a piece scheduled for
        # Tuesday and then PATCHed to Friday went out on Tuesday while the editor
        # showed Friday: one fact spelled two ways, with the wrong one on screen.
        #
        # Only the rows that are genuinely still waiting. A published row's time
        # is a record of when it went rather than a plan; a failed or cancelled
        # one is a question somebody has already settled; and ``publishing`` is
        # the one the calendar's reschedule refuses outright, because a row a
        # worker has already claimed is about to succeed and putting it back to
        # ``scheduled`` is how it gets posted a second time. So a fully published
        # piece has nothing here to move, which is why ``scheduled_for`` can stay
        # in the freeze's exemption list above.
        movable = (PublicationStatus.PENDING, PublicationStatus.SCHEDULED)
        for publication in content.publications:
            if publication.status not in movable:
                continue
            publication.scheduled_for = data["scheduled_for"]
            publication.status = (
                PublicationStatus.SCHEDULED
                if data["scheduled_for"]
                else PublicationStatus.PENDING
            )

    # The queue agrees with the column, and the column with the rows that went
    # out — see :func:`_settle_status`. After the ``scheduled_for`` pass above,
    # so a date and a demotion in the same call end with the date cleared
    # rather than a row re-armed at it.
    _settle_status(db, content, previous_status)

    db.commit()
    # Approving through here means the same thing as approving through the
    # button — a scripted caller that PATCHes the status should not need to know
    # about a second endpoint to get its piece published.
    if data.get("status") == ContentStatus.APPROVED:
        content_pipeline.release_approved(db, content)
    db.refresh(content)
    response.headers["ETag"] = _etag(content)
    return _to_detail(content)


@router.delete(
    "/{content_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a piece",
    responses=errors(*OWNED),
)
def delete_content(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Remove a piece and its publication rows from Herald.

    Not a retraction: anything already live on a platform stays live, because
    Herald has no way to unpublish it there. Archive instead if the point is
    to stop seeing it.
    """
    content = _owned_content(content_id, db, user)
    db.delete(content)
    db.commit()


# --------------------------------------------------------------------------- #
# Workflow                                                                     #
# --------------------------------------------------------------------------- #


@router.post(
    "/{content_id}/approve",
    response_model=ContentOut,
    summary="Approve a piece",
    responses=errors(*OWNED, status.HTTP_409_CONFLICT),
)
def approve_content(
    content_id: RowId,
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
    previous_status = content.status
    content.status = ContentStatus.APPROVED
    # An archived piece that went out comes back ``published``, not approved —
    # see :func:`_settle_status`. ``release_approved`` then declines it, as it
    # declines anything that is not approved.
    _settle_status(db, content, previous_status)
    db.commit()
    content_pipeline.release_approved(db, content)
    db.refresh(content)
    return _to_out(content)


class _PublishError(Exception):
    """A validation failure from :func:`_queue_publish`.

    Carries the HTTP status the single-item endpoint should raise; the bulk
    endpoint reads ``.detail`` and records it against that one item instead.
    """

    def __init__(
        self, detail: str, status_code: int = status.HTTP_400_BAD_REQUEST
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _assert_publishable(
    content: Content, payload: PublishRequestIn, owner: User
) -> datetime | None:
    """Every reason this piece cannot be queued, or the normalised publish time.

    Split out of :func:`_queue_publish` so that "would this work?" and "do it"
    are the same question asked twice rather than two questions that can drift.
    The dry-run arm of the bulk endpoints calls this and nothing else; the real
    arm reaches it through ``_queue_publish``, which writes only after it has
    returned. That is what makes a dry run *honest* — a preview built from a
    re-implementation of these checks would eventually disagree with them, and
    the moment it does is the moment somebody trusts it.

    Deliberately free of writes and of ``db``: nothing here needs a session, and
    a validator that cannot touch one cannot leave half a batch behind when the
    caller only asked what would happen. It does reach the *network*, in the
    link check — kept in rather than fenced behind a "this is only a preview"
    flag, because the pieces a dry run exists to warn about are exactly the ones
    with a dead link in them, and a preview that skips the slow check is a
    preview of a different call.

    Returns the time from :func:`app.services.scheduling.normalize` — the same
    value the caller must go on to store, so the conversion happens once.
    """
    # Archiving means "not going out", and
    # :func:`app.services.publishing_service.execute` enforces that at the last
    # gate before a platform is contacted. So arming an archived piece here
    # cannot publish it — it can only build rows a worker will cancel, after
    # this endpoint has returned 200 and told the caller their piece is queued.
    # Refusing is the honest half of that gate, and it leaves the invariant
    # total: an archived piece has nothing armed, ever. Un-archive it first.
    if content.status == ContentStatus.ARCHIVED:
        raise _PublishError(
            "This piece is archived, which means it is not going out. Take it "
            "out of the archive first.",
            status.HTTP_409_CONFLICT,
        )

    try:
        when = scheduling.normalize(payload.scheduled_for, tz=payload.timezone)
    except scheduling.ScheduleError as exc:
        # A time in the past would otherwise be picked up by the very next
        # sweep — "publish now" wearing the costume of a schedule. An
        # unresolvable ``timezone`` arrives as the same exception and takes the
        # same exit: both are the caller having named a moment Herald cannot act
        # on, and neither is a bug on this side of the request.
        raise _PublishError(str(exc), status.HTTP_422_UNPROCESSABLE_CONTENT) from exc

    unimplemented = [
        p.value for p in payload.platforms if not publishers.get_adapter(p).implemented
    ]
    if unimplemented:
        raise _PublishError(
            f"No finished adapter for: {', '.join(unimplemented)}. "
            f"Publishing works for: "
            f"{', '.join(p.value for p in publishers.implemented_platforms())}."
        )

    connected = owner.connected_platforms
    missing = [p.value for p in payload.platforms if p.value not in connected]
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

    return when


def _queue_publish(
    content: Content, payload: PublishRequestIn, db: Session, owner: User
) -> list[Publication]:
    """Validate and queue a piece for one or more platforms.

    Shared by the single-item and bulk publish endpoints so both apply the
    exact same adapter/connection/link checks. Queuing is synchronous and
    cheap; the publishing itself is a worker's job (or runs inline when
    ``CELERY_ENABLED`` is off).

    *owner* is the authenticated user, passed rather than walked to via
    ``content.project.user``: both callers have already established that this
    piece belongs to them, and the walk is two lazy hops per piece that the
    bulk endpoint pays again after every commit in its loop.

    Every refusal lives in :func:`_assert_publishable`, above, which is called
    here and nowhere in between: nothing in this function writes until that call
    has returned.
    """
    when = _assert_publishable(content, payload, owner)

    publications = publishing_service.queue(
        db,
        content,
        list(payload.platforms),
        scheduled_for=when,
        as_draft=payload.as_draft,
    )

    publishing_service.arming_approves(content)
    content.scheduled_for = when
    # Queueing a platform on a piece that had run out of them makes ``failed``
    # untrue again; this is the arm of the derivation that takes it back.
    publishing_service.sync_content_status(content)
    db.commit()

    # Before the dispatch filter below, not after: the commit expired every one
    # of these instances, so reading ``p.id`` or ``p.scheduled_for`` off one is
    # itself a SELECT. Refreshing first turns what was a round trip per
    # publication — twice over, once for the filter and once for the refresh
    # loop that used to sit at the end of this function — into a single query.
    refresh_all(db, publications)

    # Nothing scheduled goes out now. Scheduled work waits for the beat task.
    #
    # "Nothing scheduled" is per publication, not per request: an immediate
    # cross-post still leaves the syndicated copies parked behind the canonical
    # by ``publishing_service._syndication_schedule``, and dispatching those
    # here would ask a worker to publish them the moment the original went out.
    if when is None:
        _dispatch([p.id for p in publications if p.scheduled_for is None])

    return publications


@router.post(
    "/{content_id}/publish",
    response_model=list[PublicationOut],
    summary="Queue a piece for publishing",
    responses=errors(
        *OWNED,
        # 400 for a platform Herald cannot publish to — no finished adapter, or
        # no credentials on this account. 409 for dead links in the body, which
        # `allow_broken_links` overrides. 422 for a scheduled time in the past.
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
def publish_content(
    content_id: RowId,
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
        publications = _queue_publish(content, payload, db, user)
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


@router.get(
    "/{content_id}/schedule/suggestions",
    response_model=list[SlotOut],
    summary="When Herald would publish this",
    # 400 when there is nothing to suggest slots *for*: no `platforms`, nothing
    # queued, and no connected platform to fall back on.
    responses=errors(*OWNED, status.HTTP_400_BAD_REQUEST),
)
def schedule_suggestions(
    content_id: RowId,
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


@router.post(
    "/{content_id}/schedule",
    response_model=list[PublicationOut],
    summary="Put a piece on the calendar",
    responses=errors(
        *OWNED,
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
def schedule_content(
    content_id: RowId,
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
            user,
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
        refresh_all(db, publications)

    return [PublicationOut.model_validate(p) for p in publications]


@router.delete(
    "/{content_id}/schedule",
    response_model=list[PublicationOut],
    summary="Take a piece off the calendar",
    responses=errors(*OWNED),
)
def unschedule_content(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PublicationOut]:
    """Take a piece off the calendar without publishing it.

    Scheduled rows are cancelled rather than returned to ``pending``: pending
    means "go out on the next sweep", which is the opposite of what somebody
    clicking unschedule asked for. Re-publishing later re-arms the same rows.

    Cancelling the last row that could still change the piece's mind is a
    verdict on the piece, so the status is re-derived afterwards — see
    :func:`app.services.publishing_service.sync_content_status`. Without it, a
    piece that had failed on one platform and was scheduled on another stayed
    ``approved`` after this call: nothing would ever publish it and nothing
    would ever say so.
    """
    content = _owned_content(content_id, db, user)
    targets = [
        p for p in content.publications if p.status == PublicationStatus.SCHEDULED
    ]

    for publication in targets:
        publication.status = PublicationStatus.CANCELLED
        publication.scheduled_for = None
    content.scheduled_for = None
    publishing_service.sync_content_status(content)
    db.commit()
    refresh_all(db, targets)
    return [PublicationOut.model_validate(p) for p in targets]


def _rearm(content: Content, publication: Publication) -> bool:
    """Put one stopped publication back in the queue. Returns "dispatch it now".

    The three retry endpoints — one publication, one piece, a batch of pieces —
    all re-arm through here, because the interesting part is not the status
    assignment but the *hold*: a syndicated copy whose original has not
    published yet must come back ``scheduled`` behind the original rather than
    being dispatched, or it lands on a platform before there is a canonical URL
    for it to point at. See :func:`app.services.publishing_service.retry_hold`.
    Three copies of that rule is three chances for one of them to lose it.

    Does not commit — the caller decides the transaction boundary, because the
    batch endpoints re-arm many rows and a commit per row would leave a partly
    retried batch behind if one of them raised.
    """
    hold = publishing_service.retry_hold(content, publication)
    publication.status = (
        PublicationStatus.SCHEDULED if hold else PublicationStatus.PENDING
    )
    publication.attempts = 0
    publication.error = None
    publication.scheduled_for = hold
    # A retry is a human saying "send it", which on a draft is an approval —
    # a draft with an armed row is the contradiction ``_settle_status`` exists
    # to prevent, and this path used to create it.
    publishing_service.arming_approves(content)
    # The piece is no longer out of platforms to try. Without this it went on
    # reading ``failed`` while a worker was publishing it — see
    # :func:`app.services.publishing_service.sync_content_status`.
    publishing_service.sync_content_status(content)
    return hold is None


def _dispatch(publication_ids: list[int]) -> None:
    """Hand publications to a worker, or run them inline.

    The inline path exists for single-process deployments and tests. It is also
    the fallback when the broker is unreachable: losing a publish because Redis
    is down would be a silent failure of the thing the user just clicked.
    """
    if not publication_ids:
        return

    from app.tasks import publish_tasks

    # How many the broker has already accepted, so the inline fallback picks up
    # where the broker stopped instead of restarting the batch. A broker that
    # dies partway through is the case this fallback exists for, and the ids it
    # already queued are now owned by a worker. Re-running them here does the
    # whole publish attempt a second time on the request thread — read the row,
    # render, call the platform adapter — and only ``publish_one``'s conditional
    # UPDATE, one layer away, keeps that from becoming a duplicate post. That
    # guard is worth having; it is not worth spending a user-facing request on
    # work we already know was handed off. Mirrors ``webhooks.dispatch``.
    dispatched = 0
    if settings.celery_enabled:
        try:
            for publication_id in publication_ids:
                publish_tasks.publish_one.delay(publication_id)
                dispatched += 1
            return
        except Exception as exc:
            logger.warning("celery dispatch failed, publishing inline: %s", exc)

    for publication_id in publication_ids[dispatched:]:
        try:
            publish_tasks.publish_one(publication_id)
        except Exception:
            # ``publish_one`` records its own failures on the row and is
            # documented not to raise, so reaching here means something outside
            # that contract broke. The remaining ids in the batch are unrelated
            # publications and still deserve their attempt, and the caller is a
            # request thread that has already committed the rows.
            logger.exception("inline publish %s failed", publication_id)


@router.post(
    "/{content_id}/retry/{publication_id}",
    response_model=PublicationOut,
    summary="Retry a failed publication",
    # 404 covers both halves of the path; 409 is a publication that is not in a
    # state a retry means anything for.
    responses=errors(*OWNED, status.HTTP_409_CONFLICT),
)
def retry_publication(
    content_id: RowId,
    publication_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PublicationOut:
    """Re-arm one failed publication and try again.

    Immediately, except for a syndicated copy whose original has not published
    yet: that one is re-armed behind the original instead, and comes back
    ``scheduled`` for the beat sweep to pick up. Clearing ``scheduled_for`` and
    dispatching unconditionally was the last path that could put a copy on a
    platform before the piece had a canonical URL to point at — see
    :func:`app.services.publishing_service.retry_hold`.
    """
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
    # The third arming path, and the one that was missing this. ``_queue_publish``
    # states the invariant it keeps — "an archived piece has nothing armed, ever"
    # — and the calendar's reschedule keeps it for the same reason: arming a
    # piece the user has withdrawn cannot publish it, because
    # :func:`app.services.publishing_service.execute` cancels the row at the last
    # gate, so all it can do is answer 200 to a caller whose piece is not going
    # anywhere and leave the row reading ``pending`` until a worker settles it
    # back to ``cancelled``.
    #
    # Reachable from the ordinary UI, not just from the API: archiving cancels
    # the armed rows with an error on them (``ARCHIVED_ERROR``), and a cancelled
    # row with an error beside it is exactly what a Retry button is for. So the
    # review queue's Reject and the publications list's Retry disagreed, and
    # Retry appeared to win.
    if content.status == ContentStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is archived, which means it is not going out. "
            "Take it out of the archive first.",
        )

    dispatchable = _rearm(content, publication)
    db.commit()

    # A held row would be refused by ``publish_one``'s claim anyway; not
    # dispatching it saves a worker the round trip and keeps the parked row's
    # one route to a platform the beat sweep, which is where its time is checked.
    if dispatchable:
        _dispatch([publication.id])
    db.refresh(publication)
    return PublicationOut.model_validate(publication)


@router.post(
    "/{content_id}/retry",
    response_model=RetryResultOut,
    summary="Retry every stopped publication on a piece",
    responses=errors(*OWNED, status.HTTP_409_CONFLICT),
)
def retry_content(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RetryResultOut:
    """Re-arm all of this piece's failed publications at once.

    The endpoint the publications list's "Retry" actually wants: a piece that
    failed usually failed on more than one platform, and retrying it one row at
    a time is the same call repeated with ids the caller had to read out of the
    list first.

    Re-arms through the same :func:`_rearm` as the per-publication endpoint,
    which is what keeps a syndicated copy held behind its original rather than
    dispatched ahead of it.

    Answers 200 with an empty ``retried`` when nothing was retryable, rather
    than 409. The per-publication endpoint 409s because the caller named one
    row and that row cannot be retried, which is a contradiction worth
    reporting; here the caller named a *piece*, and "none of its publications
    needed retrying" is a true answer about it. ``skipped`` says which rows
    were passed over and why.
    """
    content = _owned_content(content_id, db, user)
    # Same refusal, same reason as the per-publication retry above: arming an
    # archived piece cannot publish it, so it would answer success to a caller
    # whose piece is not going anywhere.
    if content.status == ContentStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is archived, which means it is not going out. "
            "Take it out of the archive first.",
        )

    retried: list[int] = []
    skipped: list[BulkFailureOut] = []
    to_dispatch: list[int] = []
    for publication in content.publications:
        if publication.status in RETRYABLE:
            if _rearm(content, publication):
                to_dispatch.append(publication.id)
            retried.append(publication.id)
            continue
        skipped.append(
            BulkFailureOut(
                content_id=publication.id,
                reason=(
                    "Already published"
                    if publication.status == PublicationStatus.PUBLISHED
                    else f"Still {publication.status.value}"
                ),
            )
        )

    db.commit()
    _dispatch(to_dispatch)
    return RetryResultOut(content_id=content.id, retried=retried, skipped=skipped)


@router.post(
    "/{content_id}/status",
    response_model=ContentDetail,
    summary="Move a piece to a named status",
    responses=errors(
        *OWNED, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT
    ),
)
def set_content_status(
    content_id: RowId,
    payload: ContentStatusIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Promote or demote one piece, explicitly.

    Everything this does, ``PATCH /content/{id}`` with a ``status`` also does.
    It exists because that is not discoverable — a caller moving a piece
    through the pipeline should not have to know that the way to do it is a
    partial update of the whole entity — and because the transitions carry
    consequences (approving *releases*, archiving *cancels the armed rows*)
    that are worth naming in a summary rather than leaving in the PATCH's
    docstring.

    It is deliberately not a second rulebook. The settable set is
    ``ContentUpdate``'s own validator, so ``published`` and ``failed`` are
    refused here exactly as they are there; the freeze on a live piece is the
    same check; the quality floor on ``draft`` → ``review`` is the same
    :func:`_assert_review_ready`; and the release and the cancel are the same
    two calls. What is missing on purpose is ``If-Match``: this endpoint writes
    one column that the caller has stated in full, so there is no edit
    underneath it to lose — unlike the PATCH, where a blind save lands on top of
    somebody's paragraph.
    """
    content = _owned_content(content_id, db, user)
    new_status = payload.status

    # The identical rule the PATCH applies, quoted rather than re-derived: the
    # one status a published piece may take is archived. Moving it back to
    # draft says it is not published while it is still live everywhere it went.
    if (
        content.status == ContentStatus.PUBLISHED
        and new_status != ContentStatus.ARCHIVED
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This piece is live on the platforms. Archive it to hide it "
            "from Herald, or unpublish it there first.",
        )

    previous_status = content.status
    content.status = new_status
    # The same floor the PATCH applies, from the same call: this endpoint and
    # that one are two doors to the same column, and a transition refused
    # through one and accepted through the other is not a rule.
    _assert_review_ready(db, content, previous_status)
    _settle_status(db, content, previous_status)
    db.commit()
    # After the commit, as in the PATCH and the bulk approve: a release that
    # dispatches a worker must not hand it a row this request has not written.
    if new_status == ContentStatus.APPROVED:
        content_pipeline.release_approved(db, content)
    db.refresh(content)
    response.headers["ETag"] = _etag(content)
    return _to_detail(content)


@router.post(
    "/ideas/{idea_id}/write",
    response_model=ContentDetail,
    status_code=201,
    summary="Turn an idea into a draft",
    responses={
        # The only endpoint with two success codes, so both are spelled out.
        # Elsewhere FastAPI's generated "Successful Response" says enough —
        # there is one success and the summary names it — but a reader shown a
        # described 200 beside a bare 201 has been told which one is the replay
        # and left to guess the other.
        status.HTTP_201_CREATED: {
            "model": ContentDetail,
            "description": (
                "The idea was written. A draft was generated and created and "
                "the idea is now retired; a later call answers 200 with this "
                "same draft rather than writing a second one."
            ),
        },
        # The idempotent replay. Documented because a client that treats 201 as
        # "a row was created" would otherwise be wrong exactly when it matters.
        status.HTTP_200_OK: {
            "model": ContentDetail,
            "description": (
                "This idea had already been written. The draft it produced is "
                "returned as it stands; nothing was generated and nothing was "
                "created."
            ),
        },
        **errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
    },
)
@limiter.limit(settings.rate_limit_ai_generate, key_func=account_key)
def write_from_idea(
    idea_id: RowId,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Turn a suggested idea into a draft, and retire the idea.

    Idempotent per idea: an idea that has already been written returns the
    draft it produced instead of writing a second one. Generation takes long
    enough that the button looks unresponsive, and the second click used to
    spend another model call and leave the user two near-identical drafts to
    reconcile — the idea is a one-shot prompt, not a "generate again" button.

    The idea row is locked for the duration, because a check-then-write over an
    unlocked row is only idempotent against clicks far enough apart to have
    committed. Two requests in flight at once both read ``used_content_id`` as
    ``NULL``, both generate, and both write — which is exactly the outcome the
    guard exists to prevent, on exactly the double-click that motivates it. The
    lock costs nothing extra: this handler already holds its transaction open
    across the model call, so the second request waits where it would otherwise
    have spent that time generating a duplicate, and then takes the replay path.
    """
    idea = db.scalar(
        select(ContentIdea)
        .where(ContentIdea.id == idea_id)
        .with_for_update()
        # A locked read that answers from the identity map has locked the row
        # and then ignored what it says. Sessions are request-scoped here so the
        # map is empty in practice, but the whole value of the lock is reading
        # the value the other request just committed.
        .execution_options(populate_existing=True)
    )
    if idea is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Idea not found")
    project = owned_project(idea.project_id, db, user)

    if idea.used_content_id is not None:
        existing = db.get(Content, idea.used_content_id)
        if existing is not None:
            # 200, not the route's 201: the replay creates nothing.
            response.status_code = status.HTTP_200_OK
            return _to_detail(existing)
        # ``used_content_id`` is not a FK: the draft this idea produced can be
        # deleted and the idea outlives it. With nothing left to return to,
        # writing it again is the useful answer rather than a dead reference.
        idea.used_content_id = None

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
