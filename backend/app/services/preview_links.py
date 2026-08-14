"""Issuing, resolving and revoking draft preview links.

The shape is :mod:`app.services.password_reset`'s: a random URL-safe token
whose SHA-256 hash is what actually lives in the database, so a leaked dump
never hands out a working link. Two differences follow from the link being
shared rather than personal:

* It is multi-use — :func:`resolve` records a view instead of spending the
  row — so there is no ``used_at``.
* A draft can have several live links at once (one per reviewer), so issuing a
  new one does not revoke the others the way a password reset token does.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content
from app.models.mixins import as_aware, utcnow
from app.models.preview_link import PreviewLink

#: 32 bytes ≈ 43 URL-safe characters — the same budget as a password reset
#: token, for the same reason: no low-entropy guess for a hash lookup to defend
#: against.
_TOKEN_BYTES = 32


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(
    db: Session, content: Content, *, ttl_hours: int | None = None
) -> tuple[PreviewLink, str]:
    """Mint a new link for ``content``. Returns the row and the plaintext token.

    The plaintext exists only here and in whatever the caller does with it
    next — it is never stored and can never be recovered from the row.
    """
    hours = ttl_hours or settings.preview_link_default_ttl_hours
    hours = min(hours, settings.preview_link_max_ttl_hours)
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = PreviewLink(
        content_id=content.id,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + timedelta(hours=hours),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw_token


def preview_url(raw_token: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}/preview/{raw_token}"


def count_for_content(db: Session, content_id: int) -> int:
    """How many links have ever been issued for a draft, before paging."""
    return (
        db.scalar(
            select(func.count(PreviewLink.id)).where(
                PreviewLink.content_id == content_id
            )
        )
        or 0
    )


def list_for_content(
    db: Session, content_id: int, *, limit: int = 100, offset: int = 0
) -> list[PreviewLink]:
    """A page of the links issued for a draft, newest first.

    Paged because nothing bounds the underlying rows. Issuing does not prune
    and :func:`revoke` deliberately keeps the row — the view count is the only
    record that a share happened at all — so a draft passed round a team over
    a few months accumulates links indefinitely, and this listing had no
    ceiling of any kind. ``id`` breaks ties in the sort: several links minted
    in the same second are ordinary here (one per reviewer), and a page
    boundary inside a tied group otherwise drops and repeats rows.
    """
    return list(
        db.scalars(
            select(PreviewLink)
            .where(PreviewLink.content_id == content_id)
            .order_by(PreviewLink.created_at.desc(), PreviewLink.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )


def revoke(db: Session, link: PreviewLink) -> None:
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        db.commit()


def resolve(db: Session, raw_token: str) -> Content | None:
    """The draft a token points at, or ``None`` if the link isn't usable.

    Unusable covers unknown, revoked and expired alike — a reviewer with a
    dead link does not need to know which. A successful resolve counts as a
    view: it is the only signal the author gets that the link was opened.
    """
    row = db.scalar(
        select(PreviewLink).where(PreviewLink.token_hash == hash_token(raw_token))
    )
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if as_aware(row.expires_at) <= utcnow():
        return None

    row.view_count += 1
    row.last_viewed_at = utcnow()
    db.commit()

    return row.content


__all__ = [
    "count_for_content",
    "hash_token",
    "issue",
    "list_for_content",
    "preview_url",
    "resolve",
    "revoke",
]
