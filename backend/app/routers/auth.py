"""Authentication routes: register + login (JWT).

Registration is closed by default — Herald is single-user and the account comes
from ``python -m app.seed``. Every route here is rate limited: they are the only
endpoints reachable without a bearer token. See :mod:`app.ratelimit`.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
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
    PreferencesUpdate,
    Token,
    UserCreate,
    UserOut,
)
from app.schemas.errors import AUTHENTICATED, errors
from app.security import create_access_token, hash_password, verify_password
from app.services import accounts, mailer, password_reset

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
    # constant-time check costs nothing. Encoded first — compare_digest raises
    # TypeError on a non-ASCII *str*, so a token with an accent in it would be a
    # 500 rather than a 403.
    if not invite_token or not secrets.compare_digest(
        invite_token.encode("utf-8"), required.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid invite token."
        )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses=errors(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_409_CONFLICT,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_register)
def register(
    request: Request,
    response: Response,
    payload: UserCreate,
    db: Session = Depends(get_db),
) -> User:
    """Register a user, if this instance is accepting registrations.

    Closed by default — Herald is single-user and the account normally comes
    from ``python -m app.seed``. When it is open, an invite token may be
    required; see :func:`_assert_registration_allowed` for the three
    configurations and why the ambiguous one fails closed in production.

    The 403 covers all of them, deliberately: "registration is closed" and
    "your invite token is wrong" are the same answer to anyone who is not
    holding a valid token.
    """
    _assert_registration_allowed(payload.invite_token)

    # Case-insensitively, and stored lowercased: an address differing from an
    # existing one only in case is the same mailbox, so it is a duplicate here
    # and must not become a second account. See :mod:`app.services.accounts`.
    if accounts.email_taken(db, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    user = User(
        email=accounts.normalize_email(payload.email),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # `from None`, not `from exc`: the unique-constraint violation is the
        # expected outcome of a duplicate signup, not an internal fault, and
        # chaining it puts the database's own message in the traceback of a
        # response the user is meant to read as "pick another address".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        ) from None
    db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=Token,
    summary="Exchange credentials for an access token",
    responses=errors(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_login)
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    """Sign in with an email address and password.

    Sent as an OAuth2 password form, so the email goes in the ``username``
    field. The returned token is a bearer token for every other endpoint here.

    A wrong password and an unknown address are the same 401, and both cost the
    same time — the handler runs bcrypt against a dummy hash when the address
    does not exist, because "instant" versus "~100ms" is enough to enumerate
    who has an account. A deactivated account is a 403: the credentials were
    right, and telling the owner so is not a disclosure.
    """
    # OAuth2PasswordRequestForm uses ``username``; we treat it as the email, and
    # match it the way a mailbox is addressed rather than as a case-sensitive
    # string — see :mod:`app.services.accounts`.
    user = accounts.find_by_email(db, form.username)

    # Always run bcrypt even when the user does not exist — otherwise the
    # response-time difference between "email not found" (instant) and "wrong
    # password" (~100 ms of bcrypt) lets an attacker enumerate valid emails.
    _dummy_hash = "$2b$12$LJ3m4ys3Lf0mtVxlhEEPLu0PjGiHSPjxjdocRRiS/cFEhJdPmWEy."
    if not verify_password(form.password, user.hashed_password if user else _dummy_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user is None:
        # Unreachable when verify_password returned True above with the dummy
        # hash (it can't), but keeps the type checker happy and is a safety net.
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
    summary="Request a password-reset link",
    responses=errors(
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
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
    # Case-insensitive for the reason the reset exists: the person asking has
    # already failed to sign in, and matching their address only in the case it
    # happens to be stored in makes this endpoint a second dead end rather than
    # the way out of the first.
    user = accounts.find_by_email(db, payload.email)
    if user is not None and user.is_active:
        raw_token = password_reset.issue(db, user)
        subject, body = password_reset.build_email(raw_token)
        try:
            mailer.send(to=user.email, subject=subject, body=body)
        except Exception:
            # SMTP failures must not leak through — the response is deliberately
            # vague, and a 500 here reveals "this email has an account".
            logging.getLogger(__name__).exception("password reset email failed")

    return MessageOut(detail=_RESET_REQUESTED)


@router.post(
    "/password-reset/confirm",
    response_model=MessageOut,
    summary="Set a new password with a reset token",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_password_reset)
def confirm_password_reset(
    request: Request,
    response: Response,
    payload: PasswordResetConfirm,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Spend a reset token and set the new password.

    Every access token issued before this call stops working, which is what
    makes "reset the password to lock someone out" true rather than merely
    plausible. Herald's JWTs are stateless and there is no revocation list, so
    the mechanism is a timestamp on the user row and a comparison against the
    token's ``iat`` — see :attr:`app.models.user.User.tokens_valid_from`.
    """
    user = password_reset.consume(db, payload.token, payload.new_password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid, already used, or expired. "
            "Request a new one.",
        )
    return MessageOut(detail="Password updated. You can sign in with it now.")


@router.patch(
    "/me",
    response_model=UserOut,
    summary="Update account preferences",
    responses=errors(*AUTHENTICATED, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def update_me(
    payload: PreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    """Change account preferences — the display name and the weekly digest.

    ``exclude_unset`` is what makes this a PATCH: a field the client did not
    send is left alone. A field it *did* send as ``null`` is a different
    request, and for the one nullable column here it is the only way to say
    "clear my display name" — dropping every null meant a name, once set, could
    be changed but never removed. ``weekly_digest_enabled`` is ``NOT NULL``, so
    an explicit null there is refused rather than written.
    """
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("weekly_digest_enabled", True) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="weekly_digest_enabled must be true or false, not null.",
        )
    for field_, value in fields.items():
        setattr(current_user, field_, value)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get(
    "/me",
    response_model=UserOut,
    summary="The signed-in account",
    responses=errors(*AUTHENTICATED, status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_auth_read)
def me(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> User:
    """Who the bearer token belongs to, and their preferences.

    The cheapest way for a client to find out whether the token it is holding
    is still good — a reset or a deactivation invalidates one mid-session.
    """
    return current_user
