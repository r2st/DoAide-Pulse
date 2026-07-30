"""One attempt to put one piece of content on one platform.

A ``Content`` row fans out into a ``Publication`` per platform. Each carries its
own status, because "published to Dev.to, rejected by LinkedIn" is the normal
case, not an error state of the piece as a whole.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
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

if TYPE_CHECKING:
    from app.models.content import Content
    from app.models.metrics import ContentMetric


class Platform(str, Enum):
    """Every destination Herald knows how to format for.

    Membership here is not the same as being implemented — see
    ``app.services.publishers.registry`` for which adapters can actually
    publish. The enum is the vocabulary; the registry is the capability.
    """

    MEDIUM = "medium"
    DEVTO = "devto"
    HASHNODE = "hashnode"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    WORDPRESS = "wordpress"


class PublicationStatus(str, Enum):
    #: Created but not handed to a worker yet.
    PENDING = "pending"
    #: Waiting for ``scheduled_for`` to arrive.
    SCHEDULED = "scheduled"
    #: A worker has it.
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    #: Retries exhausted. ``error`` says why.
    FAILED = "failed"
    CANCELLED = "cancelled"


class Publication(Base, TimestampMixin):
    __tablename__ = "publications"
    __table_args__ = (
        # One live row per (content, platform). Re-publishing updates the row
        # rather than accumulating duplicates that would each be counted in
        # analytics.
        UniqueConstraint("content_id", "platform", name="uq_publication_content_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[Platform] = mapped_column(
        SAEnum(Platform, native_enum=False, length=30), index=True, nullable=False
    )
    status: Mapped[PublicationStatus] = mapped_column(
        SAEnum(PublicationStatus, native_enum=False, length=20),
        default=PublicationStatus.PENDING,
        index=True,
        nullable=False,
    )

    #: Stage the post on the platform instead of publishing it. Stored per
    #: publication rather than passed at dispatch time because the worker that
    #: eventually runs this row is not the caller that chose it — a scheduled
    #: publish is picked up by a beat sweep hours later, with nothing but the
    #: row to go on.
    as_draft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: When this should go out. ``None`` means as soon as a worker picks it up.
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The platform's own id and permalink, once it has one.
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(String(700))

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    content: Mapped[Content] = relationship(back_populates="publications")
    metrics: Mapped[list[ContentMetric]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )

    @property
    def is_terminal(self) -> bool:
        """True once no worker will touch this row again."""
        return self.status in {
            PublicationStatus.PUBLISHED,
            PublicationStatus.FAILED,
            PublicationStatus.CANCELLED,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<Publication id={self.id} content={self.content_id} "
            f"platform={self.platform} status={self.status}>"
        )
