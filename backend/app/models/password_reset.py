"""Single-use password reset tokens.

Only a SHA-256 *hash* of the token is stored. The plaintext exists in one place
— the link in the email — so a leaked database dump cannot be used to reset
anyone's password. SHA-256 rather than bcrypt on purpose: the token is 32 bytes
from ``secrets``, so there is no low-entropy guess for a slow hash to defend
against, and the verify happens on a lookup by hash.

Rows are kept after use rather than deleted, so a link that is clicked twice can
answer "already used" instead of "invalid" — and so there is a record of the
reset. :func:`app.services.password_reset.purge_expired` clears them out.
"""
from __future__ import annotations

# Imported at runtime: SQLAlchemy resolves Mapped[...] at class-definition time.
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Hex SHA-256 of the token handed to the user. Unique so a lookup by hash
    #: can never match two rows.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set the moment the token is spent, which is what makes it single-use.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        state = "used" if self.used_at else "live"
        return f"<PasswordResetToken id={self.id} user_id={self.user_id} {state}>"
