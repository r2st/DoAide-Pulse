"""Triggers: the things that make Pulse write something.

Pulse started with one trigger — a GitHub repo moved — hard-coded into the
autopilot. That was the right shape for a dev-blog generator and the wrong shape
for anything else: a marketing team wants a changelog when a Linear cycle
closes, a release announcement when their status page posts, a weekly roundup
because it is Friday. None of those involve a repo.

So a trigger is now a row rather than a code path. Four kinds ship:

``github``
    A watched repo moved. What the autopilot always did, with the watermark
    moved off the project and onto the trigger so one project can watch two
    repos.
``webhook``
    Something POSTed to a URL only this trigger knows. The general escape
    hatch — anything that can send an HTTP request can start a piece of content,
    which is the whole "Zapier for content" bet in one endpoint.
``rss``
    A feed gained an entry. Every changelog, status page, release feed and blog
    on the internet is already an RSS feed, so this is the widest net available
    without asking anyone to integrate anything.
``schedule``
    Time passed. No external event at all — "write the weekly roundup on
    Fridays" is a trigger, and pretending it is a special case of something else
    only makes it harder to find in the UI.

Two tables, for the same reason the outbound webhooks use two: the ``Trigger``
is the standing configuration, and each firing produces a ``TriggerEvent``
carrying its own payload and its own outcome. "Did my trigger fire, and what
happened?" is then a question with an answer that survives the trigger being
edited afterwards — and the dedupe key that stops a redelivered webhook or a
re-listed feed entry from writing the same post twice lives on the event, where
the uniqueness constraint can enforce it.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
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
    from app.models.project import Project


class TriggerKind(str, Enum):
    """Where the signal comes from. See the module docstring."""

    GITHUB = "github"
    WEBHOOK = "webhook"
    RSS = "rss"
    SCHEDULE = "schedule"

    @property
    def label(self) -> str:
        """Display name with the capitalisation each vendor actually uses.

        Spelled out rather than title-cased: ``GitHub`` and ``RSS`` both come out
        wrong from ``.title()``.
        """
        return {"github": "GitHub", "rss": "RSS", "webhook": "Webhook", "schedule": "Schedule"}[
            self.value
        ]

    @property
    def is_polled(self) -> bool:
        """True when Pulse has to go and look, rather than being told."""
        return self in (TriggerKind.GITHUB, TriggerKind.RSS, TriggerKind.SCHEDULE)


class TriggerEventStatus(str, Enum):
    """What became of one firing."""

    #: Accepted and queued, nothing decided yet.
    RECEIVED = "received"
    #: Deliberately not written about — dedupe hit, project paused, daily cap.
    SKIPPED = "skipped"
    #: A ``Content`` row exists. ``content_id`` says which.
    GENERATED = "generated"
    #: Something went wrong while acting on it. ``detail`` says what.
    FAILED = "failed"


class Trigger(Base, TimestampMixin):
    """One standing reason to write about one project."""

    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )

    kind: Mapped[TriggerKind] = mapped_column(
        SAEnum(TriggerKind, native_enum=False, length=20), nullable=False
    )
    #: What this trigger is for, in the user's words. Three RSS triggers on one
    #: project are indistinguishable by kind alone.
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Kind-specific settings — feed URL, repo, interval, the dotted paths that
    #: pull a headline out of an inbound webhook body. Validated by the schema
    #: layer on write (``app.schemas.trigger``), read defensively here: a config
    #: written by an older version must not crash a poll.
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: The public half of an inbound webhook's URL, and nothing else's. Unique
    #: across all users because it is the only thing identifying the trigger on
    #: an unauthenticated request.
    token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    #: Fernet-encrypted ``{"secret": ...}`` — the optional HMAC key an inbound
    #: sender signs with. Generated with the trigger and shown exactly once, the
    #: same contract as an outbound webhook's secret.
    encrypted_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Watermarks. ``{"last_sha": ...}`` for github, ``{"seen_ids": [...]}`` for
    #: rss. Kept per trigger rather than per project so two triggers watching
    #: two feeds do not overwrite each other's place in the queue.
    state: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fire_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Reset by any successful check. A polled trigger whose source has been
    #: unreachable this many times running is deactivated — see
    #: ``settings.trigger_disable_after_failures``.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="triggers")
    events: Mapped[list[TriggerEvent]] = relationship(
        back_populates="trigger", cascade="all, delete-orphan"
    )

    def setting(self, key: str, default: Any = None) -> Any:
        """One config value, tolerant of a config written by an older version."""
        return (self.config or {}).get(key, default)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Trigger id={self.id} kind={self.kind} name={self.name!r}>"


class TriggerEvent(Base, TimestampMixin):
    """One firing of one trigger, and what came of it."""

    __tablename__ = "trigger_events"
    __table_args__ = (
        # The dedupe guard. NULL keys do not collide in any database Pulse
        # runs on, which is what makes "this firing has nothing to dedupe on"
        # expressible: a schedule tick is always new.
        UniqueConstraint("trigger_id", "dedupe_key", name="uq_trigger_event_dedupe"),
        Index("ix_trigger_event_trigger_created", "trigger_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger_id: Mapped[int] = mapped_column(
        ForeignKey("triggers.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: What made this firing unique — a commit sha, a feed entry's guid, an id
    #: pulled out of the webhook body. ``None`` means "nothing to compare",
    #: which is the honest answer for a schedule tick and for an inbound
    #: payload with no id in it.
    dedupe_key: Mapped[str | None] = mapped_column(String(200))

    #: The one-line description of what happened, shown in the trigger's
    #: activity list. Not the content's title — this is the *cause*.
    headline: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    #: The normalized signal, as it was when the trigger fired. Frozen, for the
    #: same reason a webhook delivery freezes its body: a replay must show what
    #: happened, not what is true now.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    status: Mapped[TriggerEventStatus] = mapped_column(
        SAEnum(TriggerEventStatus, native_enum=False, length=20),
        default=TriggerEventStatus.RECEIVED,
        index=True,
        nullable=False,
    )
    #: Why it was skipped, or how it failed. Empty on success.
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Not a foreign key: an event is a log entry and must outlive a draft the
    #: user deletes. Same reasoning as ``ContentIdea.used_content_id``.
    content_id: Mapped[int | None] = mapped_column(Integer)

    trigger: Mapped[Trigger] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<TriggerEvent id={self.id} trigger={self.trigger_id} "
            f"status={self.status}>"
        )


__all__ = ["Trigger", "TriggerEvent", "TriggerEventStatus", "TriggerKind"]
