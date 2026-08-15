"""Shared model mixins, and the clock the rest of the tree reads."""
from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware "now". Used as a column default and by services."""
    return datetime.now(UTC)


def elapsed_ms(started: float) -> int:
    """Whole milliseconds since a :func:`time.monotonic` reading.

    Three things now measure how long something took and store the answer — a
    GitHub scan, an LLM attempt, and the projects router's hand-run scan — and
    the arithmetic is the same subtraction, the same scale factor and the same
    rounding each time. Written once so a duration recorded by one of them is
    comparable with a duration recorded by another, which is the whole point of
    putting them on one metrics endpoint.

    ``time.monotonic`` rather than the wall clock, because these are elapsed
    times and the wall clock can step sideways under an NTP correction.
    """
    return int(round((time.monotonic() - started) * 1000))


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
