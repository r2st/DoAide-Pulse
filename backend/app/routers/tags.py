"""The tag vocabulary: what exists, what a piece should carry, and renaming.

Three endpoints, and the third is the one with teeth.

``GET /tags`` counts the account's tags into a tree. ``POST
/tags/suggest/{content_id}`` proposes tags for one piece, ranked against that
same tree. ``POST /tags/rename`` moves a tag — and its subtree — across every
piece that carries it, which is the only write in Pulse that acts on rows
named by a *string* rather than by id. That is why it has a dry run, why the
dry run is documented before the write, and why the response says which pieces
were merged rather than merely renamed.

A router of its own rather than three more routes on ``content``: a tag is an
account-level fact here, not a field of one piece. ``GET /tags`` counts across
every project, and ``POST /tags/rename`` rewrites across every project, so
mounting them under ``/content/{content_id}`` would put an account-wide write
behind a path segment naming one row.
"""
from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import QueryRowId, RowId, get_current_user, owned_project
from app.models.content import Content
from app.models.project import Project
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.schemas.tag import (
    TagRenameIn,
    TagRenameOut,
    TagSuggestionOut,
    TagTreeOut,
)
from app.services import tags as tag_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get(
    "",
    response_model=TagTreeOut,
    summary="Every tag this account uses, as a tree",
    responses=errors(*OWNED),
)
def tag_tree(
    project_id: QueryRowId | None = Query(
        default=None, description="Count only one project's content."
    ),
    limit: int = Query(
        default=tag_service.TREE_SCAN_LIMIT,
        ge=1,
        le=tag_service.TREE_SCAN_LIMIT,
        description=(
            "How many pieces to read, newest first. The default is the ceiling; "
            "a lower value is for a caller who wants a fast, recent picture "
            "rather than a complete one. ``truncated`` says which they got."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TagTreeOut:
    """The account's tags, nested by ``parent/child``, with counts.

    ``direct`` is pieces carrying a tag exactly; ``total`` is pieces carrying it
    or anything under it, which is the number a facet list shows because
    clicking a parent shows the subtree. A parent nothing is tagged with
    directly still appears — an account whose only tag is ``guides/deployment``
    has a ``guides`` node with ``direct=0``, and hiding it would make the
    hierarchy invisible in the account that has just started using one.

    Reads one column and no article bodies. ``truncated`` is true when the
    account has more pieces than the scan is allowed to read, in which case
    every count here is a floor — worth checking before planning a rename
    against it.
    """
    if project_id is not None:
        owned_project(project_id, db, user)
    return TagTreeOut(**tag_service.tree(
        db, user.id, project_id=project_id, limit=limit
    ).as_dict())


@router.post(
    "/rename",
    response_model=TagRenameOut,
    summary="Rename or merge a tag across every piece that carries it",
    responses=errors(
        *AUTHENTICATED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit("20/hour", key_func=account_key)
def rename_tag(
    request: Request,
    response: Response,
    payload: TagRenameIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TagRenameOut:
    """Move ``old`` — and everything filed under it — onto ``new``.

    The subtree moves with the node: renaming ``guides`` to ``howto`` turns
    ``guides/deployment`` into ``howto/deployment``. Renaming onto a tag that
    already exists is a merge, not an error, and the pieces where two tags
    collapsed into one come back in ``merged``.

    Only pieces whose stored list actually changes are written and counted. A
    piece carrying ``guides-advanced`` is untouched — that is a different tag
    that happens to start with the same letters, and the separator is what
    tells them apart.

    Rate limited per account rather than per IP: this is a whole-account
    rewrite, and twenty an hour is far more than a person reorganising their
    tags will ever need and far fewer than a loop will manage.

    Do the dry run first. There is no undo — the previous tag list is not kept
    anywhere, because tags are not a tracked field on the revision trail.
    """
    # The tags column and the id, nothing else. This is a full scan of the
    # account's content by construction — the rows are named by a string, so
    # there is no index to narrow them — and pulling entities would read every
    # article body in the account to rewrite some short strings. The write goes
    # back through the ORM for the rows that change, which is a handful.
    query = (
        select(Content.id, Content.tags)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id)
        .order_by(Content.id)
    )
    if payload.project_id is not None:
        owned_project(payload.project_id, db, user)
        query = query.where(Content.project_id == payload.project_id)

    changed: list[int] = []
    merged: list[int] = []
    rewritten: dict[int, list[str]] = {}
    for row in db.execute(query).all():
        # Canonicalised before the match for the reason
        # :func:`app.services.tags.rename_in` gives: the stored lists predate
        # this feature and hold whatever the model and the editor wrote, so
        # ``Guides/Docker`` has to be found by a rename of ``guides``.
        before = tag_service.normalize_all(list(row.tags or []))
        if not any(tag_service.is_within(tag, payload.old) for tag in before):
            continue
        after = tag_service.rename_in(before, payload.old, payload.new)
        changed.append(row.id)
        rewritten[row.id] = after
        # Fewer tags out than in means two of them landed on the same value.
        # Counted from the lengths rather than by looking for ``new`` in
        # ``before``, because a merge can also happen between two *children* —
        # renaming ``guides`` onto ``howto`` merges ``guides/x`` into an
        # existing ``howto/x`` without ``howto`` itself appearing anywhere.
        if len(after) < len(before):
            merged.append(row.id)

    if payload.dry_run:
        db.rollback()
        return TagRenameOut(
            old=payload.old,
            new=payload.new,
            content_ids=changed,
            count=len(changed),
            merged=merged,
            dry_run=True,
        )

    if rewritten:
        for content in db.scalars(
            select(Content).where(Content.id.in_(rewritten))
        ):
            content.tags = rewritten[content.id]
        db.commit()
        logger.info(
            "tag rename for user %s: %r -> %r across %d piece(s)",
            user.id,
            payload.old,
            payload.new,
            len(changed),
        )

    return TagRenameOut(
        old=payload.old,
        new=payload.new,
        content_ids=changed,
        count=len(changed),
        merged=merged,
        dry_run=False,
    )


@router.post(
    "/suggest/{content_id}",
    response_model=list[TagSuggestionOut],
    summary="Tags worth adding to one piece",
    responses=errors(*OWNED),
)
def suggest_tags(
    content_id: RowId,
    limit: int = Query(
        default=tag_service.SUGGESTION_LIMIT,
        ge=1,
        le=tag_service.SUGGESTION_LIMIT,
        description="How many suggestions to return, best first.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TagSuggestionOut]:
    """Suggest tags for a piece, ranked, each with the reason it was proposed.

    The ranking prefers tags this account already uses over anything invented
    from the text, which is the whole point: a suggester that proposes a fifth
    spelling of ``deployment`` has made the vocabulary worse. Only after the
    account's own tags are exhausted does it fall back to the project's stack,
    the piece's SEO keywords, and finally to whatever words the body keeps
    repeating — the last of which is for a first piece in a new account, where
    every other source is empty.

    Suggests nothing the piece already carries at any depth: a piece tagged
    ``guides/deployment`` is offered neither that nor ``guides``. Nothing is
    written — the caller applies what it wants through ``PATCH /content/{id}``.

    A ``POST`` rather than a ``GET`` because it reads the piece's whole body and
    the account's tag scan to answer, and because it is the shape the editor
    calls on demand rather than on load. It is still read-only.
    """
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found."
        )
    return [
        TagSuggestionOut(**s.as_dict())
        for s in tag_service.suggest(db, user.id, content, limit=limit)
    ]
