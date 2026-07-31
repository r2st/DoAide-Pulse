"""User (the developer whose projects Herald promotes)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.platform_connection import PlatformConnection
    from app.models.project import Project


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Send the weekly performance summary. On by default — it is the only
    #: thing that closes the loop between publishing and knowing whether it
    #: worked — but a week with nothing in it is never sent, and nothing is
    #: sent at all unless SMTP is configured. See ``app.services.digest``.
    weekly_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    projects: Mapped[list[Project]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    connections: Mapped[list[PlatformConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def connected_platforms(self) -> list[str]:
        """Platforms with live credentials, for the settings page and guards."""
        return sorted(c.platform for c in self.connections if c.status == "connected")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r}>"
