"""Outbound webhooks: user-configured HTTP callbacks for Herald's events.

Herald will never natively integrate with everything a developer runs. A
webhook is the escape hatch — one table, one dispatcher, and suddenly a publish
can trigger a Discord announcement, a Slack message, a CI job, or a row in a
spreadsheet nobody here has heard of.

The design is a two-table one on purpose. The ``Webhook`` is the standing
subscription; each event produces a ``WebhookDelivery`` row that carries its own
payload, its own retry budget and its own record of what the far end said. That
separation is what makes the feature debuggable: "did it fire?" is a question
about deliveries, not about the endpoint, and the answer survives the endpoint
being edited or deleted afterwards.

Deliveries are also the reason nothing is fire-and-forget. A callback to a
server that is having a bad afternoon is the normal case, not an error, and a
webhook that silently drops the one event you cared about is worse than no
webhook at all.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
    from app.models.user import User


class WebhookEvent(str, Enum):
    """What Herald will call you about.

    Deliberately few. Every event here is one a person would act on — something
    went out, something failed, something needs a human — rather than a mirror
    of the internal state machine. An event nobody would write a handler for is
    a maintenance burden with no reader.
    """

    #: A piece went live somewhere for the first time. Fired once per piece,
    #: not once per platform: the interesting moment is "this is public now".
    CONTENT_PUBLISHED = "content.published"
    #: One platform gave up on one piece, retries spent. Per-publication,
    #: because "Dev.to accepted and LinkedIn refused" is the ordinary case and
    #: only the refusal is worth waking anyone for.
    PUBLICATION_FAILED = "publication.failed"
    #: The autopilot wrote something it isn't confident enough to publish. The
    #: review queue only exists if somebody opens the app; this is how they
    #: find out without doing that.
    REVIEW_PENDING = "review.pending"
    #: Sent by the "send a test" button, and never by anything else. Present in
    #: the enum so a delivery row can name its event honestly.
    PING = "webhook.ping"


#: The events a user may subscribe to. ``PING`` is excluded: it is delivered to
#: one endpoint on demand and subscribing to it would never fire.
SUBSCRIBABLE_EVENTS: tuple[WebhookEvent, ...] = (
    WebhookEvent.CONTENT_PUBLISHED,
    WebhookEvent.PUBLICATION_FAILED,
    WebhookEvent.REVIEW_PENDING,
)


class DeliveryStatus(str, Enum):
    #: Queued, or waiting out a backoff before the next attempt.
    PENDING = "pending"
    #: The far end answered 2xx.
    DELIVERED = "delivered"
    #: Retries spent, or a failure no retry could fix (a refused URL).
    FAILED = "failed"


class Webhook(Base, TimestampMixin):
    """One endpoint, subscribed to some events."""

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    url: Mapped[str] = mapped_column(String(700), nullable=False)
    #: What this endpoint is for, in the user's words. Purely a label — with
    #: three endpoints pointed at three Slack channels the URL alone tells you
    #: nothing.
    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: Event values from :class:`WebhookEvent`. Stored as strings rather than a
    #: relation because the set is small, read whole, and never queried across.
    events: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    #: Fernet-encrypted ``{"secret": ...}`` — the HMAC key the receiver verifies
    #: the signature with. Herald generates it and shows it exactly once, on
    #: create and on rotate; there is no endpoint that reads it back. A secret
    #: an API will hand out on request is a secret in name only.
    encrypted_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Reset by any success. When it reaches ``webhook_disable_after_failures``
    #: the endpoint is deactivated — an endpoint that has rejected the last
    #: twenty deliveries is gone, and continuing to queue for it turns the
    #: delivery table into a log of one dead host.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The last HTTP status seen, or ``None`` if the last attempt never got one.
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship()
    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="webhook", cascade="all, delete-orphan"
    )

    def subscribed_to(self, event: WebhookEvent | str) -> bool:
        """Whether this endpoint asked for *event*.

        Takes either spelling because the dispatcher holds an enum and the stored
        column holds strings. An endpoint with no events subscribed to matches
        nothing — the empty list is "send me nothing", not "send me everything".
        """
        value = event.value if isinstance(event, WebhookEvent) else event
        return value in (self.events or [])

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Webhook id={self.id} url={self.url!r} active={self.is_active}>"


class WebhookDelivery(Base, TimestampMixin):
    """One attempt-series to deliver one event to one endpoint."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The sweep asks "pending, and due". This covers it.
        Index("ix_webhook_delivery_status_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    webhook_id: Mapped[int] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), index=True, nullable=False
    )

    event: Mapped[WebhookEvent] = mapped_column(
        SAEnum(WebhookEvent, native_enum=False, length=40), nullable=False
    )
    #: The exact body that was (or will be) POSTed, envelope included. Frozen at
    #: emit time rather than rebuilt per attempt: a retry an hour later must
    #: send what happened, not what is true now.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, native_enum=False, length=20),
        default=DeliveryStatus.PENDING,
        index=True,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: When this delivery may next be attempted, and — while pending — the lock
    #: on it. ``None`` means "now" for a fresh row and "never again" for a
    #: terminal one; the ``status`` beside it says which. During an attempt it
    #: holds a lease a claim put there, so a concurrent sweep does not see the
    #: row as due. See :func:`app.services.webhooks.claim`.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    webhook: Mapped[Webhook] = relationship(back_populates="deliveries")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<WebhookDelivery id={self.id} webhook={self.webhook_id} "
            f"event={self.event} status={self.status}>"
        )
