"""Shared FastAPI dependencies (current-user resolution, ownership guards)."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.mixins import as_aware
from app.models.project import Project
from app.models.user import User
from app.security import decode_access_token_claims, issued_at

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """Resolve the authenticated user from the bearer token.

    A token issued before the account's ``tokens_valid_from`` is refused even
    though its signature is good and it has not expired. That is what makes a
    password reset end the sessions that were already open — see
    :attr:`app.models.user.User.tokens_valid_from`.
    """
    claims = decode_access_token_claims(token)
    if claims is None:
        raise _credentials_exc
    subject = claims.get("sub")
    try:
        user_id = int(subject)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _credentials_exc from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _credentials_exc

    cutoff = user.tokens_valid_from
    if cutoff is not None:
        minted = issued_at(claims)
        # No ``iat`` means the token cannot be shown to postdate the reset.
        # Herald always mints one, so this is a token from somewhere else.
        if minted is None:
            raise _credentials_exc
        # Compared strictly, with no tolerance window. Both sides carry
        # microseconds — see :func:`app.security.create_access_token` — so the
        # sign-in that follows a reset is genuinely later than the cutoff and
        # needs no slack to survive. Any slack here would be a window in which
        # a token minted just *before* the reset kept working, which is the one
        # token this check exists to refuse.
        if minted.timestamp() < as_aware(cutoff).timestamp():
            raise _credentials_exc
    return user


def owned_project(project_id: int, db: Session, user: User) -> Project:
    """Fetch a project, 404ing if it doesn't exist *or* isn't the user's.

    Same status either way on purpose — a 403 on someone else's id confirms the
    id exists, which is a small enumeration leak for no benefit.
    """
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project
