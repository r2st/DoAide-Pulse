"""Authentication routes: register + login (JWT).

Registration is closed by default — Pulse is single-user and the account comes
from ``python -m app.seed``. Every route here is rate limited: they are the only
endpoints reachable without a bearer token. See :mod:`app.ratelimit`.

**Every credential decision here is logged**, which the rest of the tree already
did and this file did not. The reset flow records a replayed link, an expired one
and a completed reset (:mod:`app.services.password_reset`); a machine credential
records why it was refused (:mod:`app.services.api_keys`). Sign-in recorded
nothing at all — ten wrong passwords, a deactivated account trying to get back
in, and an account being created on an install that is supposed to have one were
all indistinguishable from an idle server, and slowapi's "ratelimit exceeded"
line was the *first* trace a password-guessing run left anywhere.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.routers._patch import reject_nulls
from app.ratelimit import client_key, limiter
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
from app.security import (
    create_access_token,
    dummy_hash,
    hash_password,
    verify_password,
)
from app.services import accounts, mailer, password_reset

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Every handler below takes ``request`` (slowapi reads the caller's address off
# it) and ``response`` (slowapi writes X-RateLimit-* / Retry-After onto it, and
# raises if a limited endpoint that returns a model has nowhere to put them).


def _assert_registration_allowed(invite_token: str | None) -> None:
    """Guard :func:`register`, failing closed when disabled.

    Three states, in order:

    * disabled (the default) — nobody registers, whatever they send;
    * enabled with an invite token — the token must match exactly;
    * enabled without one — open registration for anyone.
    """
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed.",
        )

    required = settings.registration_invite_token
    if not required:
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

    Closed by default — Pulse is single-user and the account normally comes
    from ``python -m app.seed``. When it is open, an invite token may be
    required; see :func:`_assert_registration_allowed` for the three
    configurations and why the ambiguous one fails closed in production.

    The 403 covers all of them, deliberately: "registration is closed" and
    "your invite token is wrong" are the same answer to anyone who is not
    holding a valid token.
    """
    try:
        _assert_registration_allowed(payload.invite_token)
    except HTTPException as exc:
        # A refused signup is the only trace a probe of this endpoint leaves.
        # The 403 is deliberately the same for "closed", "misconfigured" and
        # "wrong token" — see the guard — so without this line an operator
        # cannot tell a bot walking the API from somebody they invited fumbling
        # a paste, and neither shows up anywhere else.
        logger.warning(
            "registration refused from %s: %s", client_key(request), exc.detail
        )
        raise

    # Case-insensitively, and stored lowercased: an address differing from an
    # existing one only in case is the same mailbox, so it is a duplicate here
    # and must not become a second account. See :mod:`app.services.accounts`.
    if accounts.email_taken(db, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered."
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
            detail="Email already registered.",
        ) from None
    db.refresh(user)
    # An account appearing on a single-user install is worth a line whatever
    # gate let it through: this is the one event here that changes who can reach
    # the rest of the API, and nothing else records that it happened.
    logger.info("account %s created from %s", user.id, client_key(request))
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
    # The stand-in hash carries the configured work factor rather than a pinned
    # one, because a dummy that is cheaper (or dearer) than the real hashes
    # re-opens that difference from the other side — see :func:`dummy_hash`.
    if not verify_password(form.password, user.hashed_password if user else dummy_hash()):
        _refused(
            request,
            "no such account" if user is None else f"wrong password for user {user.id}",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user is None:
        # Unreachable when verify_password returned True above with the dummy
        # hash (it can't), but keeps the type checker happy and is a safety net.
        _refused(request, "no such account")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        _refused(request, f"user {user.id} is deactivated")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact the administrator "
            "to reactivate it.",
        )
    logger.info("login ok for user %s from %s", user.id, client_key(request))
    return Token(access_token=create_access_token(user.id))


def _refused(request: Request, reason: str) -> None:
    """One WARNING per rejected sign-in, naming the bucket and the reason.

    The bucket is :func:`app.ratelimit.client_key` rather than the address off
    ``request.client`` — the same string slowapi puts in its own "ratelimit …
    exceeded" line, so the ten refusals and the 429 that follows them are one
    greppable run instead of two unrelated facts about the same caller.

    The reason names the *account id*, never the address that was typed. An
    operator looking at a run of these needs to know whether a real account is
    being guessed at or somebody is spraying invented mailboxes, and the id
    answers that without turning the journal into a list of who has an account
    here — which is precisely what the endpoint's own 401 refuses to disclose.

    One call in every arm, with one format, so the branches stay
    indistinguishable in cost: the response time of this endpoint is load
    bearing (see the docstring above) and a reason that was cheaper to log for
    an unknown address than for a wrong password would be a new oracle behind
    the one ``dummy_hash`` closes.
    """
    logger.warning("login refused from %s: %s", client_key(request), reason)


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
    background: BackgroundTasks,
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Email a single-use reset link.

    Always 202, and always in about the same time. Both halves are the same
    defence: the body says "if that address has an account" precisely so the
    requester cannot learn whether it does, and a handler that goes to SMTP for
    a real address and returns straight away for an unknown one answers the
    question anyway, in a channel the wording does not cover. One SMTP round
    trip is tens to hundreds of milliseconds against a database lookup's
    fraction of one — not a subtle statistical edge, a difference you can see
    in a single request.

    So the send is handed to a background task. Starlette runs it after the
    response has gone out, which puts the whole variable cost past the point
    the caller can time. Still in-process rather than on a worker, keeping what
    the inline version was right about: a reset that silently waits on a broker
    being up is worse than one that takes a second. When SMTP is unconfigured
    the link goes to the log instead — see :mod:`app.services.mailer`.

    Issuing the token stays on the request path: it needs the request-scoped
    session, which is closed by the time a background task runs, and it is two
    local statements either way.
    """
    # Case-insensitive for the reason the reset exists: the person asking has
    # already failed to sign in, and matching their address only in the case it
    # happens to be stored in makes this endpoint a second dead end rather than
    # the way out of the first.
    user = accounts.find_by_email(db, payload.email)
    if user is not None and user.is_active:
        raw_token = password_reset.issue(db, user)
        subject, body = password_reset.build_email(raw_token)
        background.add_task(_send_reset_email, user.email, subject, body)

    return MessageOut(detail=_RESET_REQUESTED)


def _send_reset_email(to: str, subject: str, body: str) -> None:
    """Deliver a reset email, swallowing whatever SMTP does about it.

    Runs after the response. Nothing is left to raise into: an exception here
    unwinds inside Starlette's background runner, not the request, so this
    logs and returns rather than letting a dead mail server become a 500 in
    the server log for a request that succeeded.
    """
    try:
        mailer.send(to=to, subject=subject, body=body)
    except Exception:
        logger.exception("password reset email failed")


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
    plausible. Pulse's JWTs are stateless and there is no revocation list, so
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
    reject_nulls(User, fields)
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
