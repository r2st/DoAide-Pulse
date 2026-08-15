"""What one call to the model chain cost, recorded where another process can read it.

Herald has no metrics backend. Everything operational it knows about the LLM
chain lives in two places, and neither survives the question being asked:

* **The log.** ``llm_router`` writes one INFO line per served completion with a
  duration on it. That is a fact about a moment, greppable only on the box, and
  gone with the journal's retention.
* **The breaker.** ``llm_router.breaker.snapshot()`` says which providers are
  standing down *right now*. It is in-process state, so the API worker answering
  ``/health/detail`` reports its own breaker — not the Celery worker's, which is
  the process that actually generates content.

"How many tokens did we spend today, and on which provider" therefore had no
answer at all, and "how long does a generation take" had one only in the sense
that a human could grep for it. Both are the questions a free-tier deployment
runs out of runway on, which is what makes them worth a table.

**A row per completion, not a counter.** A counter in memory would repeat the
breaker's mistake: the process that spends the tokens is not the process that
serves ``/api/v1/metrics``, so an in-process number would report zero on the
endpoint and the truth nowhere. A row is written by whoever made the call and
read by whoever asks, which is the only arrangement that works across the
API, the worker and beat.

**Never load-bearing.** :func:`app.services.llm_usage.record` swallows
everything it can raise. A completion that succeeded must not turn into a
failure because the accounting for it could not be written — the piece is the
product and this table is bookkeeping, and a bookkeeping error that discards
work is worse than no bookkeeping.

**Bounded by a purge, not by a cap.** ``purge_old_llm_usage`` drops rows past
``llm_usage_retention_days`` on the same daily maintenance tick that prunes
webhook deliveries. The alternative — keeping N rows — would make the retention
window depend on how busy the week was, and the window is the thing the metrics
endpoint's numbers are relative to.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import utcnow

#: Column widths, named here for the same reason
#: :data:`app.models.content.TITLE_MAX_LENGTH` is: something outside this module
#: writes them, and PostgreSQL answers an over-long INSERT with an exception
#: that is not ``IntegrityError`` and which nothing in the tree catches.
#:
#: Both are wider than the values Herald produces today — provider names are
#: single words and model ids are vendor slugs — because the value comes from
#: configuration a user edits, and a truncating write is how bookkeeping starts
#: lying.
PROVIDER_MAX_LENGTH = 40
MODEL_MAX_LENGTH = 120
#: What the completion was for — ``"content"``, ``"headlines"``, ``"ideas"``.
#: Free text rather than an enum: a new caller must be able to appear without a
#: migration, and nothing branches on this value. It is a grouping key.
PURPOSE_MAX_LENGTH = 40


class LLMUsage(Base):
    """One completion attempt that reached a provider, served or refused.

    Append-only, like :class:`app.models.metrics.ContentMetric` and for the same
    reason: the series is the point. "Groq served 40 generations at 900ms and
    then started taking nine seconds" is a question about a shape over time, and
    a row that got updated in place cannot answer it.
    """

    __tablename__ = "llm_usage"
    __table_args__ = (
        # Every query on this table is "the last N hours, grouped by provider" —
        # see `app.services.llm_usage.summary`. Leading with the timestamp is
        # what makes the window a range scan instead of a scan of the table.
        Index("ix_llm_usage_created_provider", "created_at", "provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )

    provider: Mapped[str] = mapped_column(String(PROVIDER_MAX_LENGTH), nullable=False)
    model: Mapped[str] = mapped_column(String(MODEL_MAX_LENGTH), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(PURPOSE_MAX_LENGTH), default="", nullable=False
    )

    #: Whether the provider produced usable text. A failed attempt is recorded
    #: too — the duration it burned is real, and a provider whose every call
    #: times out at 90 seconds is invisible in a table of successes only.
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Wall-clock for the one attempt, as :mod:`app.services.llm_router` measured
    #: it. Milliseconds because that is the resolution anyone reads it at, and an
    #: integer because a float here would invite an average of averages.
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # NULL rather than zero when the provider did not report usage — several of
    # the free compat layers omit the block entirely, and a zero would drag a
    # sum down while looking like a measurement. Same reasoning as
    # ``ContentMetric``'s nullable counters.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<LLMUsage {self.provider}/{self.model} "
            f"tokens={self.total_tokens} ok={self.ok}>"
        )


__all__ = [
    "MODEL_MAX_LENGTH",
    "PROVIDER_MAX_LENGTH",
    "PURPOSE_MAX_LENGTH",
    "LLMUsage",
]
