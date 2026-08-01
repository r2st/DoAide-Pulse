"""Trigger request and response models.

The interesting work here is config validation. ``config`` is a JSON column
because four kinds want four different shapes, and a JSON column with no
validation is a support ticket generator: a feed trigger with no ``feed_url``
fails silently an hour later, in a Celery log nobody is reading. So each kind
declares what it needs and what it accepts, and the check happens at the edge
where the error can still be shown to the person who made it.

The signing secret follows the outbound-webhook contract exactly: it appears in
the response that creates it and the one that rotates it, and in no other.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.content import ContentType
from app.models.trigger import TriggerEventStatus, TriggerKind

#: Keys each kind understands, and which of them it cannot work without.
#: Unknown keys are refused rather than ignored — a typo'd ``feed_uri`` that is
#: silently accepted is a trigger that will never fire and never say why.
REQUIRED_CONFIG: dict[TriggerKind, tuple[str, ...]] = {
    TriggerKind.RSS: ("feed_url",),
    TriggerKind.WEBHOOK: (),
    TriggerKind.SCHEDULE: (),
    TriggerKind.GITHUB: (),
}

#: Accepted by every kind. ``content_type`` picks what gets written,
#: ``instructions`` is standing direction for the model, ``every_hours`` is the
#: poll interval.
COMMON_CONFIG: tuple[str, ...] = ("content_type", "instructions", "every_hours")

ALLOWED_CONFIG: dict[TriggerKind, tuple[str, ...]] = {
    TriggerKind.RSS: COMMON_CONFIG + ("feed_url",),
    TriggerKind.WEBHOOK: COMMON_CONFIG
    + ("headline_path", "summary_path", "url_path", "dedupe_path", "require_signature"),
    TriggerKind.SCHEDULE: COMMON_CONFIG + ("topic", "hour_utc"),
    TriggerKind.GITHUB: COMMON_CONFIG + ("repo", "commit_threshold"),
}

#: How long a schedule may sleep. A year is a mistyped number, not a plan.
MAX_INTERVAL_HOURS = 24 * 365


def validate_config(kind: TriggerKind, config: dict[str, Any]) -> dict[str, Any]:
    """Return *config* checked against *kind*, or raise ``ValueError``."""
    body = dict(config or {})

    allowed = set(ALLOWED_CONFIG[kind])
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValueError(
            f"A {kind.value} trigger has no setting called "
            f"{', '.join(unknown)}. It accepts: {', '.join(sorted(allowed))}."
        )

    missing = [key for key in REQUIRED_CONFIG[kind] if not str(body.get(key) or "").strip()]
    if missing:
        raise ValueError(f"A {kind.value} trigger needs {', '.join(missing)}.")

    if "content_type" in body and body["content_type"]:
        try:
            ContentType(str(body["content_type"]).lower())
        except ValueError as exc:
            raise ValueError(
                f"{body['content_type']!r} is not a content type. Choose from: "
                f"{', '.join(t.value for t in ContentType)}."
            ) from exc

    if "every_hours" in body and body["every_hours"] is not None:
        try:
            hours = float(body["every_hours"])
        except (TypeError, ValueError) as exc:
            raise ValueError("every_hours must be a number of hours.") from exc
        if not 0 < hours <= MAX_INTERVAL_HOURS:
            raise ValueError(
                f"every_hours must be between 0 and {MAX_INTERVAL_HOURS} "
                "(one year)."
            )
        body["every_hours"] = hours

    if body.get("hour_utc") is not None:
        try:
            hour = int(body["hour_utc"])
        except (TypeError, ValueError) as exc:
            raise ValueError("hour_utc must be an hour of the day, 0-23.") from exc
        if not 0 <= hour <= 23:
            raise ValueError("hour_utc must be an hour of the day, 0-23.")
        body["hour_utc"] = hour

    if body.get("commit_threshold") is not None:
        try:
            threshold = int(body["commit_threshold"])
        except (TypeError, ValueError) as exc:
            raise ValueError("commit_threshold must be a whole number.") from exc
        if threshold < 1:
            raise ValueError("commit_threshold must be at least 1.")
        body["commit_threshold"] = threshold

    if kind == TriggerKind.RSS:
        # Validated here as well as at poll time. The point is the error
        # message: "that host is private" while the user is looking at the form
        # beats a failed check an hour later.
        from app.services.feeds import FeedError, validate_feed_url

        try:
            body["feed_url"] = validate_feed_url(str(body["feed_url"]))
        except FeedError as exc:
            raise ValueError(str(exc)) from exc

    return body


class TriggerCreate(BaseModel):
    project_id: int
    kind: TriggerKind
    name: str = Field(default="", max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @model_validator(mode="after")
    def _check_config(self) -> TriggerCreate:
        self.config = validate_config(self.kind, self.config)
        return self


class TriggerUpdate(BaseModel):
    """Every field optional — this is a PATCH.

    ``config`` is replaced wholesale rather than merged. A merge cannot express
    "remove this setting", and a half-updated config is harder to reason about
    than one the client sends complete.
    """

    name: str | None = Field(default=None, max_length=120)
    config: dict[str, Any] | None = None
    is_active: bool | None = None

    @field_validator("config")
    @classmethod
    def _shape(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # Kind-aware validation needs the stored row, so the router calls
        # ``validate_config`` itself. This only rejects the obviously wrong.
        if v is not None and not isinstance(v, dict):
            raise ValueError("config must be an object")
        return v


class TriggerOut(BaseModel):
    id: int
    project_id: int
    kind: TriggerKind
    name: str
    is_active: bool
    config: dict[str, Any]
    #: Present only for inbound webhook triggers. The URL a sender POSTs to,
    #: absolute, because a relative one is useless to paste into another system.
    inbound_url: str | None = None
    #: Whether a signing secret exists. The secret itself never appears here.
    has_secret: bool = False
    last_checked_at: datetime | None = None
    last_fired_at: datetime | None = None
    fire_count: int
    consecutive_failures: int
    last_error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TriggerCreated(TriggerOut):
    """A created (or re-secreted) trigger, plus its signing secret, shown once."""

    secret: str


class TriggerEventOut(BaseModel):
    id: int
    trigger_id: int
    headline: str
    status: TriggerEventStatus
    detail: str
    content_id: int | None = None
    dedupe_key: str | None = None
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class TriggerKindOut(BaseModel):
    """One kind and how to configure it, for the trigger-builder UI."""

    kind: str
    label: str
    description: str
    required_config: list[str]
    optional_config: list[str]


__all__ = [
    "ALLOWED_CONFIG",
    "COMMON_CONFIG",
    "MAX_INTERVAL_HOURS",
    "REQUIRED_CONFIG",
    "TriggerCreate",
    "TriggerCreated",
    "TriggerEventOut",
    "TriggerKindOut",
    "TriggerOut",
    "TriggerUpdate",
    "validate_config",
]
