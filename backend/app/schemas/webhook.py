"""Webhook request and response models.

The secret appears in exactly two responses — the one that creates it and the
one that rotates it — and in no other. Everything else describes the endpoint
and what has happened to it.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.webhook import SUBSCRIBABLE_EVENTS, DeliveryStatus, WebhookEvent


def _validate_events(events: list[WebhookEvent]) -> list[WebhookEvent]:
    if not events:
        raise ValueError(
            "Subscribe to at least one event. Available: "
            + ", ".join(e.value for e in SUBSCRIBABLE_EVENTS)
            + "."
        )
    unsupported = [e.value for e in events if e not in SUBSCRIBABLE_EVENTS]
    if unsupported:
        raise ValueError(
            f"Not subscribable: {', '.join(unsupported)}. "
            f"Choose from: {', '.join(e.value for e in SUBSCRIBABLE_EVENTS)}."
        )
    # Deduplicated so a caller sending the same event twice does not produce a
    # subscription list that looks like a bug when it is read back.
    return list(dict.fromkeys(events))


class WebhookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=700)
    events: list[WebhookEvent] = Field(max_length=20)
    description: str = Field(default="", max_length=200)

    _events = field_validator("events")(_validate_events)


class WebhookUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=8, max_length=700)
    events: list[WebhookEvent] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    @field_validator("events")
    @classmethod
    def _check(cls, v: list[WebhookEvent] | None) -> list[WebhookEvent] | None:
        return None if v is None else _validate_events(v)


class WebhookOut(BaseModel):
    id: int
    url: str
    description: str
    events: list[str]
    is_active: bool
    consecutive_failures: int
    last_delivery_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookCreated(WebhookOut):
    """A created (or re-secreted) webhook, plus the secret, shown once.

    A separate model rather than an optional field on :class:`WebhookOut` so
    that no list or detail endpoint can ever grow the secret by accident.
    """

    secret: str


class WebhookDeliveryOut(BaseModel):
    id: int
    webhook_id: int
    event: WebhookEvent
    status: DeliveryStatus
    attempts: int
    response_status: int | None = None
    error: str | None = None
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime
    payload: dict

    model_config = {"from_attributes": True}


class WebhookEventOut(BaseModel):
    """One subscribable event, for the settings UI's checkbox list."""

    event: str
    description: str
