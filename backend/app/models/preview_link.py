"""Shareable, expiring, read-only links to a single draft.

Same shape as :class:`app.models.password_reset.PasswordResetToken` and the
same reasoning: only a SHA-256 hash of the token is stored, so a leaked
database dump does not hand out working preview links. Unlike a reset token
this one is multi-use — a reviewer opening it twice is the normal case, not a
replay — so there is no ``used_at``. ``revoked_at`` is the author taking it
back early; ``expires_at`` is it lapsing on its own.
"""
from __future__ import annotations

# Imported at runtime: SQLAlchemy resolves Mapped[...] at class-definition time.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin, as_aware, utcnow

if TYPE_CHECKING:
    from app.models.content import Content


class PreviewLink(Base, TimestampMixin):
    __tablename__ = "preview_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Hex SHA-256 of the token in the URL. Unique so a lookup by hash can
    #: never match two rows.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set the moment the author revokes it. Distinct from expiry so the UI can
    #: tell "you took this back" from "this timed out".
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How many times a viewer has opened the link, and when the last one was —
    #: the only signal an author gets that a link was actually used.
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    content: Mapped[Content] = relationship(back_populates="preview_links")

    @property
    def is_live(self) -> bool:
        """Whether this link would still open the draft right now.

        Revocation beats expiry: a link taken back is dead whatever its TTL said.
        Both are checked here rather than in the query so a row already loaded
        can be judged without a round trip — :func:`app.services.preview_links.resolve`
        makes the same two checks against the database for the real decision.
        """
        if self.revoked_at is not None:
            return False
        return as_aware(self.expires_at) > utcnow()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        state = "revoked" if self.revoked_at else "live" if self.is_live else "expired"
        return f"<PreviewLink id={self.id} content_id={self.content_id} {state}>"
