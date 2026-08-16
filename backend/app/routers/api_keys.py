"""Managing the machine credentials in ``api_keys``.

Minting, listing, rotating and revoking — all of it authenticated with a session
token, because creating a credential is something a person does. What the
credential then *reaches* is :mod:`app.routers.machine`, which authenticates
with the key itself; see that module for why the two halves are not one file.

Nothing here ever returns a token except the two routes that create one. The
stored form is a digest, so a token that is lost is not recoverable — it is
rotated. That is the correct shape and it is worth being deliberate about: an
endpoint that could show an existing key's plaintext would make every one of
these rows worth stealing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import ListOffset, RowId, get_current_user, owned_project
from app.models.api_key import ALL_SCOPES, ApiKey, ApiKeyScope
from app.models.user import User
from app.ratelimit import limiter
from app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyRotate,
    ApiKeyRotated,
    ApiKeyScopeOut,
)
from app.schemas.errors import OWNED, errors
from app.services import api_keys

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

#: What each scope permits, for the key-creation form. Kept beside the
#: management routes rather than on the enum because it is copy, and copy
#: belongs next to the endpoint that serves it — the same argument
#: ``webhooks.EVENT_DESCRIPTIONS`` makes.
SCOPE_DESCRIPTIONS: dict[ApiKeyScope, str] = {
    ApiKeyScope.CONTENT_READ: (
        "List the project's pieces and their publication state. No bodies, no "
        "drafts in progress — the shipping record only."
    ),
    ApiKeyScope.CONTENT_WRITE: (
        "File an idea for a human or the autopilot to pick up. Cannot write, "
        "edit or publish a piece."
    ),
    ApiKeyScope.ANALYTICS_READ: (
        "Read the project's engagement totals — views, reactions, comments, "
        "shares — across every platform it publishes to."
    ),
}

#: Ceiling on live keys per project. Every key is an independently revocable
#: credential, and a list nobody can read is a list nobody audits. Revoked keys
#: do not count: the history is the point of keeping them.
MAX_KEYS_PER_PROJECT = 20


def _owned_key(key_id: RowId, db: Session, user: User) -> ApiKey:
    """Fetch a key, 404ing if it isn't this user's — see ``deps.owned_project``."""
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
        )
    return key


@router.get(
    "/scopes",
    response_model=list[ApiKeyScopeOut],
    summary="Scopes a key can carry",
    responses=errors(status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_public_read)
def list_scopes(request: Request, response: Response) -> list[ApiKeyScopeOut]:
    """Every scope, and what granting it permits.

    Reachable without a token, and limited like the rest of the anonymous
    surface for the reason ``/webhooks/events`` gives: whether an endpoint is
    public and whether it is limited should not be two separate questions.

    Both parameters are slowapi's — see the note on ``webhooks.list_events``.
    """
    return [
        ApiKeyScopeOut(scope=scope.value, description=SCOPE_DESCRIPTIONS[scope])
        for scope in ALL_SCOPES
    ]


@router.get(
    "",
    response_model=list[ApiKeyOut],
    summary="Your API keys",
    responses=errors(*OWNED),
)
def list_api_keys(
    response: Response,
    project_id: int | None = Query(default=None),
    include_revoked: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ApiKeyOut]:
    """This account's keys, optionally narrowed to one project.

    Revoked keys are hidden unless asked for. They are never deleted — a key
    that authenticated something for six months is part of the audit trail, and
    an audit trail with the interesting rows removed is not one.

    Never carries a token: the plaintext exists only in the response that
    minted it.
    """
    query = select(ApiKey).where(ApiKey.user_id == user.id)
    if project_id is not None:
        # Resolved through the ownership guard so somebody else's id 404s
        # rather than returning an empty list — same contract as /triggers.
        owned_project(project_id, db, user)
        query = query.where(ApiKey.project_id == project_id)
    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))

    total = db.scalar(
        select(func.count()).select_from(query.with_only_columns(ApiKey.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)

    rows = db.scalars(query.order_by(ApiKey.id).limit(limit).offset(offset))
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key",
    responses=errors(*OWNED, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def create_api_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiKeyCreated:
    """Mint a key on a project. The token is in this response and nowhere else."""
    project = owned_project(payload.project_id, db, user)

    live = (
        db.scalar(
            select(func.count(ApiKey.id)).where(
                ApiKey.project_id == project.id, ApiKey.revoked_at.is_(None)
            )
        )
        or 0
    )
    if live >= MAX_KEYS_PER_PROJECT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"At most {MAX_KEYS_PER_PROJECT} live keys per project.",
        )

    try:
        expires_at = api_keys.expiry_from_days(payload.expires_in_days)
        key, token = api_keys.mint(
            db,
            project=project,
            name=payload.name,
            scopes=payload.scopes,
            expires_at=expires_at,
        )
    except api_keys.ApiKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    db.commit()
    db.refresh(key)
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), token=token)


@router.post(
    "/{key_id}/rotate",
    response_model=ApiKeyRotated,
    summary="Replace a key with a new one",
    responses=errors(*OWNED, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def rotate_api_key(
    key_id: RowId,
    payload: ApiKeyRotate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiKeyRotated:
    """Mint a replacement with the same name and scopes, and retire this one.

    With ``grace_hours: 0`` — the default — the old key stops working the
    instant this returns. With a grace window, both work until it closes, which
    is what makes rotating a key that is deployed in six places something other
    than a coordinated outage.
    """
    key = _owned_key(key_id, db, user)
    try:
        replacement, token = api_keys.rotate(db, key, grace_hours=payload.grace_hours)
    except api_keys.ApiKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return ApiKeyRotated(
        key=ApiKeyCreated(
            **ApiKeyOut.model_validate(replacement).model_dump(), token=token
        ),
        replaced=ApiKeyOut.model_validate(key),
    )


@router.delete(
    "/{key_id}",
    response_model=ApiKeyOut,
    summary="Revoke a key",
    responses=errors(*OWNED),
)
def revoke_api_key(
    key_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiKeyOut:
    """Stop the key working, permanently.

    Returns the revoked row rather than a 204: the caller wants the timestamp,
    and the row stays in the listing under ``include_revoked``. Idempotent —
    revoking twice keeps the first timestamp.
    """
    key = _owned_key(key_id, db, user)
    return ApiKeyOut.model_validate(api_keys.revoke(db, key))


__all__ = ["MAX_KEYS_PER_PROJECT", "SCOPE_DESCRIPTIONS", "router"]
