"""Shared model mixins."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware "now". Used as a column default and by services."""
    return datetime.now(UTC)


def as_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    Timestamp columns are ``DateTime(timezone=True)`` and everything written to
    them goes through :func:`utcnow`, so a value that comes back naive did so
    because the backend dropped the offset — SQLite does, Postgres does not.
    Comparing one against an aware "now" raises ``TypeError``, which means the
    comparison would work in production and fail in the tests, or the reverse.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
