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
from app.schemas.auth import (
    MessageOut,
    PasswordResetConfirm,
    PasswordResetRequest,
    Token,
    UserCreate,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password
from app.services import mailer, password_reset

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


# --------------------------------------------------------------------------- #
# Password reset                                                               #
# --------------------------------------------------------------------------- #

#: Returned whatever happened: address unknown, account deactivated, SMTP down.
#: Anything more specific turns this endpoint into a way to ask "does this person
#: have an account here", and the requester cannot act on the difference anyway.
_RESET_REQUESTED = (
    "If that address has an account, a reset link is on its way. "
    "The link works once and expires shortly."
)


@router.post(
    "/password-reset",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(settings.rate_limit_password_reset)
def request_password_reset(
    request: Request,
    response: Response,
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Email a single-use reset link.

    Always 202. Sending happens inline rather than on a worker: it is one SMTP
    round trip, and a reset that silently waits on a broker being up is worse
    than one that takes a second. When SMTP is unconfigured the link goes to the
    log instead — see :mod:`app.services.mailer`.
    """
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is not None and user.is_active:
        raw_token = password_reset.issue(db, user)
        subject, body = password_reset.build_email(raw_token)
        mailer.send(to=user.email, subject=subject, body=body)

    return MessageOut(detail=_RESET_REQUESTED)


@router.post("/password-reset/confirm", response_model=MessageOut)
@limiter.limit(settings.rate_limit_password_reset)
def confirm_password_reset(
    request: Request,
    response: Response,
    payload: PasswordResetConfirm,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Spend a reset token and set the new password.

    Note what this does *not* do: existing access tokens keep working until they
    expire. Herald's JWTs are stateless and there is no revocation list, so
    "reset the password to lock someone out" is not something this endpoint can
    honestly promise. ACCESS_TOKEN_EXPIRE_MINUTES is the bound.
    """
    user = password_reset.consume(db, payload.token, payload.new_password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid, already used, or expired. "
            "Request a new one.",
        )
    return MessageOut(detail="Password updated. You can sign in with it now.")


@router.get("/me", response_model=UserOut)
@limiter.limit(settings.rate_limit_auth_read)
def me(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> User:
    return current_user
