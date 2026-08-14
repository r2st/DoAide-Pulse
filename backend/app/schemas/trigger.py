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

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

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

#: How long each free-text setting may be.
#:
#: ``config`` is a JSON column, so nothing downstream of it truncates on the way
#: in — the caps have to be here or they do not exist. Two of these values leave
#: the process:
#:
#: ``instructions``
#:     Concatenated verbatim into the model prompt by
#:     :func:`app.services.content_generator.build_brief`. Unbounded, it is a
#:     way to spend a whole context window (and the budget behind it) on one
#:     trigger firing. 2000 is what ``GenerateRequest.instructions`` already
#:     allows for the same text typed into the generate form, and the two should
#:     not disagree about the same field.
#: ``topic``
#:     Becomes the signal headline for a schedule tick, and ``record()``
#:     truncates that to the ``trigger_events.headline`` column at 300. Anything
#:     longer was never stored, so accepting it only promises something the
#:     activity list will not show.
#:
#: The ``*_path`` settings are dotted paths into an inbound body. They are not
#: prompt input, but they are unbounded strings on an authenticated write, and a
#: path deeper than any real JSON document is a typo rather than a mapping.
MAX_CONFIG_LENGTHS: dict[str, int] = {
    "instructions": 2000,
    "topic": 300,
    "headline_path": 200,
    "summary_path": 200,
    "url_path": 200,
    "dedupe_path": 200,
}

#: ``owner/name`` as GitHub itself defines it: owners are alphanumeric with
#: single internal hyphens (39 max), repository names allow dot and underscore
#: too (100 max).
#:
#: Anchored, and deliberately not a "does it look roughly right" check, because
#: this value is interpolated straight into an API path —
#: ``f"/repos/{full_name}/commits"`` in :mod:`app.services.github_client`. A
#: value with a ``?`` in it appends a query string to a URL that already has
#: one; a value with a further ``/`` reaches a different endpoint entirely.
#:
#: The ``(?!\.{1,2}$)`` is the one rule GitHub's own charset does not imply:
#: ``.`` and ``..`` are legal spellings under the name charset (``.github`` is a
#: real repository) and are exactly the two that traverse out of ``/repos/``
#: once a client normalizes the path.
_REPO_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
    r"/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$"
)


def validate_config(kind: TriggerKind, config: dict[str, Any] | None) -> dict[str, Any]:
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

    for key, cap in MAX_CONFIG_LENGTHS.items():
        value = body.get(key)
        if value is None:
            continue
        if len(str(value)) > cap:
            raise ValueError(f"{key} must be {cap} characters or fewer.")

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

    if body.get("repo") is not None:
        # Blank is allowed and means something: the github poller falls back to
        # the project's own repo_url when the trigger names no repo of its own.
        repo = str(body["repo"]).strip()
        if repo and not _REPO_RE.match(repo):
            raise ValueError(
                f"{repo!r} is not a GitHub repository. Give it as owner/name, "
                "e.g. r2st/Herald."
            )
        body["repo"] = repo

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

    There is deliberately no ``config`` validator here. Kind-aware validation
    needs the stored row to know *which* kind's rules apply, so the router calls
    :func:`validate_config` itself once it has loaded the trigger; the annotation
    below is what rejects a non-object, and it does so before any validator of
    ours would run.
    """

    name: str | None = Field(default=None, max_length=120)
    config: dict[str, Any] | None = None
    is_active: bool | None = None


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
    """One firing. ``payload`` is present only when it was asked for.

    The frozen signal on an event is the whole inbound body — ``MAX_INBOUND_BYTES``
    allows 128 KB of it — and the firing history pages 200 events at a time. The
    activity list renders a headline, a status and a detail; it has never read
    the payload, so shipping every byte of every firing to draw a list of
    one-liners was the endpoint's entire response size for none of its content.

    ``None`` rather than ``{}`` when it is left out, because the two mean
    different things: an empty dict is a firing that genuinely carried nothing,
    which is a real state for a schedule tick. Ask for it with
    ``?include_payload=true``, or fetch one event on its own — see
    ``GET /triggers/{trigger_id}/events/{event_id}``, which always includes it.
    """

    id: int
    trigger_id: int
    headline: str
    status: TriggerEventStatus
    detail: str
    content_id: int | None = None
    dedupe_key: str | None = None
    payload: dict | None = None
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
    "MAX_CONFIG_LENGTHS",
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
