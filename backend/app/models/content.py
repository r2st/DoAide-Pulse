"""A generated piece of content, and the states it moves through.

Content is stored once, in Markdown, and adapted per platform at publish time
(see ``app.services.publishers``). That way a post that goes to Dev.to and
LinkedIn is genuinely the same piece rather than two divergent copies.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.publication import Publication


class ContentType(str, Enum):
    """What kind of piece this is. Drives the prompt and the target length."""

    TUTORIAL = "tutorial"
    ANNOUNCEMENT = "announcement"
    FEATURE_SPOTLIGHT = "feature_spotlight"
    COMPARISON = "comparison"
    HOW_TO = "how_to"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class ContentStatus(str, Enum):
    """draft → review → approved → published, plus the two exits.

    ``review`` is where the autopilot parks anything it isn't confident about;
    ``approved`` means a human said yes but it hasn't gone out yet (either it's
    scheduled, or the publish is still in flight).
    """

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"


#: Rough target length per type, in words. The generator passes this to the
#: model and the token budget is derived from it — a tutorial that comes back
#: at tweet length is a failure, and so is an announcement that runs to 2000
#: words nobody reads.
TARGET_WORDS: dict[ContentType, int] = {
    ContentType.TUTORIAL: 1200,
    ContentType.ANNOUNCEMENT: 450,
    ContentType.FEATURE_SPOTLIGHT: 700,
    ContentType.COMPARISON: 1000,
    ContentType.HOW_TO: 900,
}


class Content(Base, TimestampMixin):
    __tablename__ = "content"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )

    content_type: Mapped[ContentType] = mapped_column(
        SAEnum(ContentType, native_enum=False, length=30), nullable=False
    )
    status: Mapped[ContentStatus] = mapped_column(
        SAEnum(ContentStatus, native_enum=False, length=20),
        default=ContentStatus.DRAFT,
        index=True,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    #: The canonical body. Markdown — every adapter converts *from* this.
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: One- or two-sentence summary; doubles as the social blurb.
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- SEO ----
    meta_description: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    #: Tags as the blogging platforms mean them (Dev.to caps at 4, Medium at 5).
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    #: Set when the piece is published somewhere first and syndicated after —
    #: the adapters send it as rel=canonical so the copies don't compete.
    canonical_url: Mapped[str | None] = mapped_column(String(500))

    # ---- Provenance ----
    #: Which provider/model actually wrote it, for the "what works" analysis.
    generated_by_provider: Mapped[str | None] = mapped_column(String(40))
    generated_by_model: Mapped[str | None] = mapped_column(String(120))
    #: The model's own read on whether this is ready to go out unreviewed.
    #: Drives the auto-publish gate; ``None`` for anything a human wrote.
    confidence: Mapped[float | None] = mapped_column(Float)
    #: What prompted it — {"kind": "release", "tag": "v1.2.0", ...} or
    #: {"kind": "manual", "user_id": 1}. Kept for the analytics breakdown.
    source: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # ---- Scheduling ----
    #: When this should go out. ``None`` means "on approval, immediately".
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="content")
    publications: Mapped[list[Publication]] = relationship(
        back_populates="content", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def word_count(self) -> int:
        return len(self.body_markdown.split())

    @property
    def read_minutes(self) -> int:
        """Reading time at ~220wpm, floored at one minute."""
        return max(1, round(self.word_count / 220))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Content id={self.id} title={self.title!r} status={self.status}>"


class ContentIdea(Base, TimestampMixin):
    """A subject worth writing about that nobody has written yet.

    The repo monitor produces these; the calendar's "suggested" slots consume
    them. Kept separate from ``Content`` so an unwritten idea never shows up in
    a content list, an export, or a word count.
    """

    __tablename__ = "content_ideas"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_type: Mapped[ContentType] = mapped_column(
        SAEnum(ContentType, native_enum=False, length=30), nullable=False
    )
    headline: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    #: Set once the idea has been turned into a piece, so it stops being
    #: suggested. Not a FK — an idea outlives the draft that a user deletes.
    used_content_id: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ContentIdea id={self.id} headline={self.headline!r}>"
