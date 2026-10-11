"""Engagement snapshots for a published piece.

Append-only: each poll writes a new row rather than updating one. Storing the
series instead of the latest number is what makes "this post got 400 views in
its first two days and nothing since" answerable, and it costs a few hundred
rows a month.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin, utcnow

if TYPE_CHECKING:
    from app.models.publication import Publication


class ContentMetric(Base, TimestampMixin):
    __tablename__ = "content_metrics"
    __table_args__ = (
        Index("ix_content_metrics_pub_captured", "publication_id", "captured_at"),
        Index("ix_content_metrics_pub_latest", "publication_id", "id", postgresql_using="btree"),
        CheckConstraint("views IS NULL OR views >= 0", name="ck_metric_views_nonneg"),
        CheckConstraint("reads IS NULL OR reads >= 0", name="ck_metric_reads_nonneg"),
        CheckConstraint("clicks IS NULL OR clicks >= 0", name="ck_metric_clicks_nonneg"),
        CheckConstraint("reactions IS NULL OR reactions >= 0", name="ck_metric_reactions_nonneg"),
        CheckConstraint("comments IS NULL OR comments >= 0", name="ck_metric_comments_nonneg"),
        CheckConstraint("shares IS NULL OR shares >= 0", name="ck_metric_shares_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )

    # Not every platform reports every field; a missing one stays NULL rather
    # than becoming a zero that would drag an average down.
    views: Mapped[int | None] = mapped_column(Integer)
    reads: Mapped[int | None] = mapped_column(Integer)
    clicks: Mapped[int | None] = mapped_column(Integer)
    reactions: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    #: Boosts, reposts, retweets — someone putting the post in front of their
    #: own audience. Kept apart from ``reactions`` because it is the only
    #: engagement signal that grows the reach rather than measuring it, and on
    #: the social platforms it is the number worth optimising for.
    shares: Mapped[int | None] = mapped_column(Integer)

    publication: Mapped[Publication] = relationship(back_populates="metrics")

    @property
    def engagement(self) -> int:
        """Every interaction the platform reported, treating missing as zero."""
        return (
            (self.reactions or 0)
            + (self.comments or 0)
            + (self.clicks or 0)
            + (self.shares or 0)
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ContentMetric pub={self.publication_id} views={self.views}>"
