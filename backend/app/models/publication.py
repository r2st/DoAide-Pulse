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
    Index,
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

    Lookup is case-insensitive, so the member *name* resolves as well as its
    value. ``autopilot_platforms`` is a plain JSON column rather than a typed
    enum column, and rows exist that hold ``"DEVTO"`` — SQLAlchemy stores enum
    names, so anything written through the ORM's enum machinery round-trips in
    upper case while the API writes ``.value`` in lower case. Reading one of
    those rows back through ``ProjectOut`` was a 500 on ``GET /projects``.
    Accepting both spellings on the way in costs nothing; ``.value`` is still
    the only spelling that ever goes out, which is what the frontend reads.
    """

    @classmethod
    def _missing_(cls, value: object) -> Platform | None:
        if isinstance(value, str):
            folded = value.strip().lower()
            for member in cls:
                if member.value == folded:
                    return member
        return None

    MEDIUM = "medium"
    DEVTO = "devto"
    HASHNODE = "hashnode"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    WORDPRESS = "wordpress"
    MASTODON = "mastodon"
    BLUESKY = "bluesky"
    #: Not a platform so much as a destination: a commit to the repo a blog is
    #: built from. Usually the one that should own the canonical URL, since it
    #: is the only copy on a domain the user controls.
    GIT = "git"
    #: Email. The only destination Herald publishes to that cannot be taken
    #: back, which is why its adapter treats a send and a draft as genuinely
    #: different operations rather than one flag on the same call.
    BUTTONDOWN = "buttondown"


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


#: How much of a platform's own post id and permalink the row can hold.
#:
#: Named rather than inlined because the values are written from a *response
#: body* — every adapter records whatever the platform put in its ``url`` or
#: ``id`` field, and three of those platforms (WordPress, Mastodon, Bluesky)
#: are servers the user typed the address of. A value over the column width
#: raises ``StringDataRightTruncation`` on PostgreSQL inside the same commit
#: that records the post as published, which is the one place in the tree where
#: a rollback is worse than a lost field: the post is already live, so the
#: publication is re-armed and published again. See
#: ``app.services.publishing_service.execute``.
EXTERNAL_ID_MAX_LENGTH = 200
EXTERNAL_URL_MAX_LENGTH = 700


class Publication(Base, TimestampMixin):
    __tablename__ = "publications"
    __table_args__ = (
        # One live row per (content, platform). Re-publishing updates the row
        # rather than accumulating duplicates that would each be counted in
        # analytics.
        UniqueConstraint("content_id", "platform", name="uq_publication_content_platform"),
        # The beat sweep queries "status IN (pending, scheduled) WHERE
        # scheduled_for <= now()". This covers it.
        Index("ix_publication_status_scheduled", "status", "scheduled_for"),
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
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    #: The platform's own id and permalink, once it has one.
    external_id: Mapped[str | None] = mapped_column(String(EXTERNAL_ID_MAX_LENGTH))
    external_url: Mapped[str | None] = mapped_column(String(EXTERNAL_URL_MAX_LENGTH))

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    #: How long the last attempt's call to the platform took, in milliseconds.
    #:
    #: The *platform call* specifically — not the row's lifetime and not the
    #: task's. A publication can sit scheduled for a week and a worker can spend
    #: a second on credentials and canonical URLs either side of the request;
    #: none of that is the platform's latency, and mixing it in would make the
    #: number unusable for the one question it answers: which destination is
    #: slow, and is it getting slower. See
    #: ``app.services.publishing_service.execute``, which starts the clock after
    #: the credentials are decrypted and stops it in a ``finally``.
    #:
    #: Written for a *failed* attempt as well as a successful one, which is the
    #: half that matters most — a platform taking forty seconds to refuse a post
    #: is the reason a worker hits its soft time limit, and the successful rows
    #: alone would show that platform's latency as its best days only.
    #:
    #: Nullable, and NULL is not zero: it means no attempt has reached a
    #: platform yet — a pending row, a scheduled one, or one the circuit breaker
    #: parked before anything was sent. The metrics endpoint counts the timed
    #: rows separately for exactly that reason.
    duration_ms: Mapped[int | None] = mapped_column(Integer)

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
