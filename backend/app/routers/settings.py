"""Platform connections — the settings page.

Credentials go in and never come out. Every response here is built from
:func:`app.services.publishers.capabilities` plus the connection's status,
never from the stored blob.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.user import User
from app.schemas.settings import ConnectionCreate, ConnectionOut, PlatformCapability
from app.services import publishers
from app.services.crypto import CredentialEncryptionError, encrypt_credentials
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/platforms", response_model=list[PlatformCapability])
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


@router.put("/connections", response_model=ConnectionOut)
def upsert_connection(
    payload: ConnectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Save credentials for a platform, verifying them first.

    Verification is not optional. Storing an unverified token means the user
    finds out it is wrong when a scheduled post fails overnight, which is the
    worst possible moment.
    """
    adapter = publishers.get_adapter(payload.platform)

    expected = {f.key for f in adapter.credential_fields}
    required = {f.key for f in adapter.credential_fields if f.required}
    supplied = {k for k, v in payload.credentials.items() if str(v).strip()}

    unknown = supplied - expected
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown credential field(s) for {adapter.display_name}: "
            f"{', '.join(sorted(unknown))}",
        )
    missing = required - supplied
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} needs: {', '.join(sorted(missing))}",
        )

    try:
        display_name = adapter.verify(payload.credentials)
    except NotImplementedAdapter as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
    except CredentialError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PublishError as exc:
        # The platform is having a bad day — the credentials might be fine.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        encrypted = encrypt_credentials(dict(payload.credentials))
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


@router.post("/connections/{platform}/verify", response_model=ConnectionOut)
def verify_connection(
    platform: Platform,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Re-check stored credentials, e.g. after a platform reports them invalid."""
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
    try:
        connection.display_name = adapter.verify(
            decrypt_credentials(connection.encrypted_credentials)
        )
        connection.status = ConnectionStatus.CONNECTED
        connection.last_verified_at = utcnow()
        connection.last_error = None
    except (CredentialError, CredentialEncryptionError) as exc:
        connection.status = ConnectionStatus.INVALID
        connection.last_error = str(exc)
    except (PublishError, NotImplementedAdapter) as exc:
        # Leave the status alone: an unreachable platform is not proof the
        # credentials are bad, and flipping to INVALID would make the user
        # re-enter a working token.
        connection.last_error = str(exc)

    db.commit()
    db.refresh(connection)
    return ConnectionOut.model_validate(connection)


@router.delete("/connections/{platform}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    platform: Platform,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
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
    db.delete(connection)
    db.commit()
