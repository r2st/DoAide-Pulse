"""Issuing and spending password reset tokens.

The shape is the conventional one, and the details are where the security lives:

* The token is 32 bytes from :mod:`secrets`, URL-safe, and only its SHA-256 hash
  is stored — see :class:`app.models.password_reset.PasswordResetToken`.
* Requesting a reset invalidates any token already outstanding for that user, so
  a second request cannot be used to keep an old link alive.
* Spending a token marks it used in the same transaction as the password change,
  and revokes every other token for the user.
* Every failure mode on the request side — unknown address, inactive account,
  SMTP down — produces the same response, because a different one turns the
  endpoint into a check for "does this person have an account here".
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.mixins import as_aware, utcnow
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.security import hash_password

logger = logging.getLogger(__name__)

#: 32 bytes ≈ 43 URL-safe characters. Comfortably beyond guessing, still short
#: enough that the link survives an email client's line wrapping.
_TOKEN_BYTES = 32


def hash_token(raw_token: str) -> str:
    """The stored form of a reset token. SHA-256, hex.

    No salt and no work factor, deliberately: the token is 32 random bytes, so
    there is no low-entropy guess for either to defend against, and the lookup
    has to be a single indexed comparison rather than a scan.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(db: Session, user: User) -> str:
    """Revoke the user's outstanding tokens, mint a new one, return the plaintext.

    The plaintext is returned rather than stored: this is the only moment it
    exists, and the caller's job is to put it in an email and forget it.
    """
    revoke_all(db, user.id)

    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=utcnow()
            + timedelta(minutes=settings.password_reset_token_ttl_minutes),
        )
    )
    db.commit()
    return raw_token


def revoke_all(db: Session, user_id: int) -> None:
    """Mark every live token for a user as used, without committing."""
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=utcnow())
    )


def reset_link(raw_token: str) -> str:
    """Where the email points. The SPA reads the token out of the query string."""
    return f"{settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"


def build_email(raw_token: str) -> tuple[str, str]:
    """Subject and plain-text body for the reset message."""
    minutes = settings.password_reset_token_ttl_minutes
    body = (
        "Someone asked to reset the password for your Herald account.\n\n"
        f"{reset_link(raw_token)}\n\n"
        f"The link works once and expires in {minutes} minutes.\n"
        "If this wasn't you, nothing has changed and you can ignore this email.\n"
    )
    return f"Reset your {settings.app_name} password", body


def consume(db: Session, raw_token: str, new_password: str) -> User | None:
    """Spend a token and set the new password. ``None`` if it is not usable.

    Unusable covers unknown, already spent, expired, and belonging to a
    deactivated account. The caller reports all of them identically — a token
    the holder cannot use is not worth explaining in detail.
    """
    row = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(raw_token)
        )
    )
    if row is None:
        return None
    if row.used_at is not None:
        logger.info("password reset token %s replayed", row.id)
        return None
    if as_aware(row.expires_at) <= utcnow():
        logger.info("password reset token %s expired", row.id)
        return None

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    now = utcnow()
    user.hashed_password = hash_password(new_password)
    # Every access token issued before this moment stops working. Without it a
    # reset changes what the *next* sign-in needs and nothing else, so somebody
    # resetting because their account was taken leaves the attacker's bearer
    # token live for the rest of its lifetime — see
    # :attr:`app.models.user.User.tokens_valid_from`.
    user.tokens_valid_from = now
    row.used_at = now
    # Any other link that was issued before this one must not survive the reset.
    revoke_all(db, user.id)
    db.commit()
    logger.info("password reset completed for user %s", user.id)
    return user


def purge_expired(db: Session) -> int:
    """Delete spent and expired rows. Returns how many went. Not scheduled —
    a single-user install accumulates a handful of rows a year."""
    rows = list(
        db.scalars(
            select(PasswordResetToken).where(
                (PasswordResetToken.used_at.is_not(None))
                | (PasswordResetToken.expires_at <= utcnow())
            )
        )
    )
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


__all__ = [
    "build_email",
    "consume",
    "hash_token",
    "issue",
    "purge_expired",
    "reset_link",
    "revoke_all",
]
