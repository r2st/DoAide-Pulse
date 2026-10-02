"""Content revisions: history, diff, restore."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import ROW_ID_MAX, ListOffset, RowId, get_current_user
from app.models.content import Content
from app.models.revision import ContentRevision
from app.models.user import User
from app.schemas.errors import OWNED, errors
from app.schemas.revision import (
    FieldDiffOut,
    RevisionDetail,
    RevisionDiffOut,
    RevisionListOut,
    RevisionOut,
    RevisionRestoreOut,
)
from app.services import revisions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/content", tags=["revisions"])

#: A page of history. Small, because the sidebar this feeds shows about ten
#: entries and the rest is a "load more" nobody presses.
DEFAULT_PAGE = 20


def _owned(content_id: RowId, db: Session, user: User) -> Content:
    """The piece, or a 404 whether it is missing or somebody else's.

    The per-router ownership helper, matching the idiom the rest of the tree
    uses — 404 rather than 403, because a 403 on another account's row confirms
    the row exists. See ``test_tenant_isolation``, which sweeps every route with
    a path parameter and would fail if this returned anything else.
    """
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


def _revision_or_404(db: Session, content: Content, revision: int) -> ContentRevision:
    """One stored revision of this piece, or a 404 naming what is available."""
    stored = revisions.get(db, content, revision)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"This piece has no version {revision}. It is currently at version "
                f"{content.version}; older versions are kept "
                f"{revisions.RETENTION} deep."
            ),
        )
    return stored


@router.get(
    "/{content_id}/revisions",
    response_model=RevisionListOut,
    summary="A piece's edit history",
    responses=errors(*OWNED),
)
def list_revisions(
    content_id: RowId,
    response: Response,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=100),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RevisionListOut:
    """Past versions of this piece, newest first.

    Without their bodies — see :mod:`app.schemas.revision` for why, which is the
    same reason ``GET /templates`` leaves out template bodies. Fetch one version
    from ``GET /content/{id}/revisions/{revision}`` when the user opens it.

    A piece that has never been edited has no revisions, and that is the honest
    answer rather than a synthetic entry for the current text: there is nothing
    to go back to. The current text is on the piece itself and is never
    duplicated here.

    ``total`` counts every stored revision, not this page, so the sidebar can
    say "12 versions". ``retained`` is the ceiling, so it can also say why
    version 3 is gone.
    """
    content = _owned(content_id, db, user)
    total = revisions.count(db, content)
    items = revisions.history(db, content, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return RevisionListOut(
        items=[RevisionOut.model_validate(item) for item in items],
        total=total,
        retained=revisions.RETENTION,
    )


@router.get(
    "/{content_id}/revisions/{revision}",
    response_model=RevisionDetail,
    summary="One past version, with its text",
    responses=errors(*OWNED),
)
def get_revision(
    content_id: RowId,
    revision: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RevisionDetail:
    """One stored version of this piece, in full.

    ``revision`` is the piece's ``version`` at the time, which is the same number
    the ``ETag`` carries — so a client holding a stale copy can ask for exactly
    the text it was editing.
    """
    content = _owned(content_id, db, user)
    return RevisionDetail.model_validate(_revision_or_404(db, content, revision))


@router.get(
    "/{content_id}/revisions/{revision}/diff",
    response_model=RevisionDiffOut,
    summary="What changed between two versions",
    responses=errors(*OWNED),
)
def diff_revision(
    content_id: RowId,
    revision: RowId,
    against: int | None = Query(
        default=None,
        ge=1,
        le=ROW_ID_MAX,
        description=(
            "The version to compare against. Omit to compare with the piece as "
            "it stands now, which is what the history sidebar asks for."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RevisionDiffOut:
    """How ``revision`` differs from the current text, or from ``against``.

    Changed fields only: a diff listing seven fields of which one differs buries
    the answer. Bodies and excerpts come back as unified-diff lines, clipped at
    :data:`app.services.revisions.DIFF_LINE_LIMIT` with a line saying so; short
    fields come back as before and after, because a patch header over a
    thirty-character string is noise.

    The direction is always older → newer as the caller named them, and nothing
    here checks that ``revision`` really is the older of the two. Comparing
    forwards is a legitimate thing to ask for and inverting it silently would
    make the ``+`` and ``-`` lines mean the opposite of what the caller expects.
    """
    content = _owned(content_id, db, user)
    older = _revision_or_404(db, content, revision)
    newer = _revision_or_404(db, content, against) if against is not None else content
    return RevisionDiffOut(
        from_revision=revision,
        to_revision=against,
        fields=[FieldDiffOut(**item.as_dict()) for item in revisions.diff(older, newer)],
    )


@router.post(
    "/{content_id}/revisions/{revision}/restore",
    response_model=RevisionRestoreOut,
    summary="Put an earlier version back",
    responses=errors(*OWNED, status.HTTP_409_CONFLICT),
)
def restore_revision(
    content_id: RowId,
    revision: RowId,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RevisionRestoreOut:
    """Restore this piece's text to an earlier version.

    Not destructive, and that is the whole design: the text being replaced is
    snapshotted first, so the response carries a ``previous_revision`` the user
    can restore in turn. Undoing a restore is the same button.

    Only the seven fields a human edits move — see
    :data:`app.services.revisions.TRACKED_FIELDS`. The piece's status, its
    schedule and its publication rows are untouched, so a restore cannot
    un-publish anything or re-arm a queue that has already run. The slug follows
    the title through the same :func:`unique_content_slug` the PATCH route uses,
    rather than being restored from a stored value another piece may since have
    taken.

    Refused with a 409 on a published piece, in the same words ``PATCH`` refuses
    to edit one: the text is live on the platforms, and changing it here would
    make Pulse disagree with what a reader can see without changing anything a
    reader can see. Archived pieces restore fine — archiving means "stop showing
    me this", not "this went out".

    The new ``version`` comes back in the body and in the ``ETag`` header, so an
    editor that restores can carry on saving; without it the next PATCH sends a
    stale ``If-Match`` and is refused with a 412.
    """
    content = _owned(content_id, db, user)
    stored = _revision_or_404(db, content, revision)
    try:
        previous = revisions.restore(db, content, stored, author_user_id=user.id)
    except revisions.RevisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    previous_revision = previous.revision
    db.commit()
    db.refresh(content)
    response.headers["ETag"] = f'"{content.version}"'
    return RevisionRestoreOut(
        restored_revision=revision,
        previous_revision=previous_revision,
        version=content.version,
    )
