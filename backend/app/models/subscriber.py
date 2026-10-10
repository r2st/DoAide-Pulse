"""Newsletter subscribers collected via the embed widget."""
from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin

EMAIL_MAX_LENGTH = 320


class Subscriber(Base, TimestampMixin):
    __tablename__ = "subscribers"
    __table_args__ = (
        UniqueConstraint("email", name="uq_subscriber_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False)
    source: Mapped[str] = mapped_column(String(100), default="embed", nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Subscriber id={self.id} email={self.email!r}>"
