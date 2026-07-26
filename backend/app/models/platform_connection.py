"""Stored credentials for one user on one publishing platform.

Credentials are a JSON blob rather than named columns because every platform
wants something different — Dev.to a single API key, WordPress a site URL plus
an application password, LinkedIn an OAuth token plus a URN. The blob is
encrypted at rest (see ``app.services.crypto``); only the non-secret handle is
stored in the clear so the settings page has something to show.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin
from app.models.publication import Platform

if TYPE_CHECKING:
    from app.models.user import User


class ConnectionStatus(str, Enum):
    CONNECTED = "connected"
    #: Credentials were rejected on last use. Kept rather than deleted so the
    #: settings page can say "reconnect" instead of silently forgetting.
    INVALID = "invalid"
    DISCONNECTED = "disconnected"


class PlatformConnection(Base, TimestampMixin):
    __tablename__ = "platform_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_connection_user_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[Platform] = mapped_column(
        SAEnum(Platform, native_enum=False, length=30), nullable=False
    )
    status: Mapped[ConnectionStatus] = mapped_column(
        SAEnum(ConnectionStatus, native_enum=False, length=20),
        default=ConnectionStatus.CONNECTED,
        nullable=False,
    )

    #: Fernet-encrypted JSON. Never returned by the API — the schemas expose
    #: ``display_name`` and ``status`` only.
    encrypted_credentials: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The public handle/account the credentials belong to, e.g. "@r2st" or a
    #: blog URL. Safe to display.
    display_name: Mapped[str | None] = mapped_column(String(200))

    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="connections")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformConnection id={self.id} user={self.user_id} "
            f"platform={self.platform} status={self.status}>"
        )
