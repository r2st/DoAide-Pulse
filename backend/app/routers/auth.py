"""Authentication routes: register + login (JWT).

Registration is closed by default — Herald is single-user and the account comes
from ``python -m app.seed``. Every route here is rate limited: they are the only
endpoints reachable without a bearer token. See :mod:`app.ratelimit`.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.ratelimit import limiter
from app.schemas.auth import Token, UserCreate, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Every handler below takes ``request`` (slowapi reads the caller's address off
# it) and ``response`` (slowapi writes X-RateLimit-* / Retry-After onto it, and
# raises if a limited endpoint that returns a model has nowhere to put them).


def _assert_registration_allowed(invite_token: str | None) -> None:
    """Guard :func:`register`, failing closed on every ambiguous configuration.

    Three states, in order:

    * disabled (the default) — nobody registers, whatever they send;
    * enabled with an invite token — the token must match exactly;
    * enabled without one — allowed in development, refused in production,
      because "enabled" there almost certainly means someone flipped the flag
      and forgot the token, and the failure mode is an open signup endpoint.
    """
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed.",
        )

    required = settings.registration_invite_token
    if not required:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is closed: no invite token is configured.",
            )
        return

    # compare_digest over ==: the comparison is against a secret, and a short
    # constant-time check costs nothing.
    if not invite_token or not secrets.compare_digest(invite_token, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid invite token."
        )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.rate_limit_register)
def register(
    request: Request,
    response: Response,
    payload: UserCreate,
    db: Session = Depends(get_db),
) -> User:
    _assert_registration_allowed(payload.invite_token)

    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
@limiter.limit(settings.rate_limit_login)
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    # OAuth2PasswordRequestForm uses ``username``; we treat it as the email.
    user = db.scalar(select(User).where(User.email == form.username))
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
@limiter.limit(settings.rate_limit_auth_read)
def me(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> User:
    return current_user
