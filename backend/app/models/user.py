"""User (the developer whose projects Pulse promotes)."""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.api_key import ApiKey
    from app.models.platform_connection import PlatformConnection
    from app.models.project import Project
    from app.models.template import ContentTemplate


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
    #: Access tokens issued before this moment are refused. Stamped whenever the
    #: password changes, which is what makes a reset actually end the other
    #: sessions rather than only stopping the next sign-in.
    #:
    #: Pulse's JWTs are stateless and there is no revocation list, so without
    #: this a user who resets because their account was compromised leaves the
    #: attacker's token working for the rest of its lifetime — the reset reads
    #: as "you are locked out" and means nothing of the sort. One column and one
    #: comparison against ``iat`` buys the guarantee without a session table.
    #:
    #: ``NULL`` means "never changed", and every token is accepted. That is the
    #: right default for rows that predate the column: the alternative signs
    #: everybody out on deploy for a threat that has not happened.
    tokens_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    projects: Mapped[list[Project]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    connections: Mapped[list[PlatformConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    #: Reusable content shapes. Owned by the user rather than the project so one
    #: template can serve every project — see ``app.models.template``.
    templates: Mapped[list[ContentTemplate]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    #: Machine credentials. Owned by the user *and* narrowed to one project —
    #: see ``app.models.api_key``. The relationship is here so deleting an
    #: account takes its keys with it rather than leaving rows that
    #: authenticate as nobody.
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def connected_platforms(self) -> list[str]:
        """Platforms with live credentials, for the settings page and guards."""
        return sorted(c.platform for c in self.connections if c.status == "connected")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r}>"
