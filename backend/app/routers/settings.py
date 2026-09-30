"""Platform connections — the settings page.

Credentials go in and never come out. Every response here is built from
:func:`app.services.publishers.capabilities` plus the connection's status,
never from the stored blob.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.schemas.settings import ConnectionCreate, ConnectionOut, PlatformCapability
from app.services import publishers
from app.services.crypto import CredentialEncryptionError, encrypt_credentials
from app.services.errors import clip_error, redact
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "/platforms",
    response_model=list[PlatformCapability],
    summary="Every publishing platform and its connection state",
    responses=errors(*AUTHENTICATED),
)
def list_platforms(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[PlatformCapability]:
    """Every platform, what it needs, and whether this user has connected it."""
    connections = {
        c.platform: c
        for c in db.scalars(
            select(PlatformConnection).where(PlatformConnection.user_id == user.id)
        )
    }
    return [
        PlatformCapability(
            **capability,
            connection=(
                ConnectionOut.model_validate(connections[Platform(capability["platform"])])
                if Platform(capability["platform"]) in connections
                else None
            ),
        )
        for capability in publishers.capabilities()
    ]


@router.put(
    "/connections",
    response_model=ConnectionOut,
    summary="Connect a platform, or replace its credentials",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        *AUTHENTICATED,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_501_NOT_IMPLEMENTED,
        status.HTTP_502_BAD_GATEWAY,
    ),
)
@limiter.limit(settings.rate_limit_outbound_probe, key_func=account_key)
def upsert_connection(
    payload: ConnectionCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Save credentials for a platform, verifying them first.

    Verification is not optional. Storing an unverified token means the user
    finds out it is wrong when a scheduled post fails overnight, which is the
    worst possible moment.

    Which is also why this carries the outbound-probe budget: verification is a
    synchronous request to the platform, made from Herald's address on the
    caller's say-so, and the credentials it verifies are the ones in the body
    rather than anything already stored — so it is the one endpoint here that
    answers "is this token good?" for a token the caller just made up. Left
    unlimited it is a credential-stuffing oracle against the platforms, run
    from Herald. On the Git adapter it is worse than that: ``GitAdapter._token``
    falls back to the install's shared ``GITHUB_TOKEN`` when the connection
    carries none, so an unlimited loop here spends the same single budget that
    ``rate_limit_repo_scan`` exists to protect, and answers 403 to every account
    on the install once it is gone.
    """
    adapter = publishers.get_adapter(payload.platform)

    expected = {f.key for f in adapter.credential_fields}
    required = {f.key for f in adapter.credential_fields if f.required}

    # Every key the caller sent, whatever its value. Deriving this from the
    # non-blank entries — which is what it used to do — meant a misspelled field
    # whose value happened to be blank or whitespace was not "supplied", so it
    # was not unknown either: the check that exists to catch a typo skipped
    # exactly the payloads a typo produces. The key was then encrypted and
    # stored with the rest, and the connection looked correctly configured
    # while the real field sat empty.
    unknown = set(payload.credentials) - expected
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown credential field(s) for {adapter.display_name}: "
            f"{', '.join(sorted(unknown))}",
        )

    # Blank means unset, and it means that all the way down rather than only
    # here. A whitespace value stored for an optional field is worse than a
    # missing one: `credentials.get("branch") or "main"` returns the whitespace,
    # because " " is true.
    credentials = {
        key: str(value).strip()
        for key, value in payload.credentials.items()
        if str(value).strip()
    }

    missing = required - set(credentials)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} needs: {', '.join(sorted(missing))}",
        )
    if not credentials:
        # Only reachable for an adapter that requires nothing, where `missing`
        # is empty by definition. Storing `{}` would present as connected.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} needs at least one credential.",
        )

    try:
        display_name = adapter.verify(credentials)
    except NotImplementedAdapter as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
    except CredentialError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PublishError as exc:
        # The platform is having a bad day — the credentials might be fine.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        encrypted = encrypt_credentials(credentials)
    except CredentialEncryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == payload.platform,
        )
    )
    if connection is None:
        connection = PlatformConnection(user_id=user.id, platform=payload.platform)
        db.add(connection)

    connection.encrypted_credentials = encrypted
    connection.display_name = display_name
    connection.status = ConnectionStatus.CONNECTED
    connection.last_verified_at = utcnow()
    connection.last_error = None

    db.commit()
    db.refresh(connection)
    return ConnectionOut.model_validate(connection)


@router.post(
    "/connections/{platform}/verify",
    response_model=ConnectionOut,
    summary="Re-check stored credentials",
    responses=errors(*OWNED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_outbound_probe, key_func=account_key)
def verify_connection(
    platform: Platform,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Re-check stored credentials, e.g. after a platform reports them invalid.

    Always 200 once the connection exists — the verdict is in the returned
    ``status`` and ``last_error``, not in the status code. A platform that is
    merely unreachable leaves ``status`` alone rather than marking the
    connection invalid: an outage is not proof the token is bad, and flipping it
    would make the user re-enter one that works.

    Carries the outbound-probe budget for the reason
    :func:`upsert_connection` does — one call is one synchronous request to the
    platform — minus the stuffing-oracle half, since the credentials here are
    the stored ones. What is left is still a button that turns one HTTP request
    from the caller into one from Herald, and on the Git adapter one that can
    spend the install's shared ``GITHUB_TOKEN``.
    """
    from app.services.crypto import decrypt_credentials

    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == platform,
        )
    )
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not connected"
        )

    adapter = publishers.get_adapter(platform)
    # Held so the failure arms can strip them back out of whatever the platform
    # said. ``verify`` runs against a token the user has just pasted in, and its
    # message lands in ``last_error`` — a plaintext column rendered on the
    # settings page, next to the encrypted copy of the same value. Empty when
    # decryption is what failed, which is the one case there is nothing to
    # remove. See ``publishing_service._redact_credentials`` for the same guard
    # on the publish path.
    credentials: dict = {}
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
        connection.display_name = adapter.verify(credentials)
        connection.status = ConnectionStatus.CONNECTED
        connection.last_verified_at = utcnow()
        connection.last_error = None
    except (CredentialError, CredentialEncryptionError) as exc:
        connection.status = ConnectionStatus.INVALID
        connection.last_error = clip_error(
            redact(str(exc), publishers.secret_values(adapter, credentials))
        )
    except (PublishError, NotImplementedAdapter) as exc:
        # Leave the status alone: an unreachable platform is not proof the
        # credentials are bad, and flipping to INVALID would make the user
        # re-enter a working token.
        connection.last_error = clip_error(
            redact(str(exc), publishers.secret_values(adapter, credentials))
        )

    db.commit()
    db.refresh(connection)
    return ConnectionOut.model_validate(connection)


@router.delete(
    "/connections/{platform}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect a platform",
    responses=errors(*OWNED),
)
def delete_connection(
    platform: Platform,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Forget a platform's credentials.

    A hard delete, unlike the rest of Herald: there is nothing here worth
    keeping, and "deleted" has to mean the ciphertext is gone. Content already
    published there is untouched — this removes the ability to publish again,
    not the record of having done so.
    """
    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user.id,
            PlatformConnection.platform == platform,
        )
    )
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not connected"
        )
    logger.warning(
        "user %s disconnected %s — every queued publication for it now fails "
        "as unconnected until it is reconnected",
        user.id,
        platform.value,
    )
    db.delete(connection)
    db.commit()
