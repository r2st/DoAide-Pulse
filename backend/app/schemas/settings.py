"""Platform-connection and preference schemas.

Credential *values* never appear in a response model. The only direction they
travel is inbound.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.platform_connection import ConnectionStatus
from app.models.publication import Platform
from app.schemas.limits import (
    MAX_CREDENTIAL_FIELDS,
    CredentialKey,
    CredentialValue,
)
from app.services import languages


class ConnectionCreate(BaseModel):
    platform: Platform
    #: Keys match the adapter's ``credential_fields``. Validated against them by
    #: the router, so a typo is rejected rather than silently stored.
    #:
    #: Bounded on all three axes, because a mapping has more than one. The
    #: ``min_length=1`` here was a floor on the entry count and nothing else:
    #: neither key nor value had a ceiling, and the router's own checks run
    #: after parsing, so an oversized value reached ``adapter.verify`` — which
    #: puts it on the network — before anything had looked at its size.
    credentials: dict[CredentialKey, CredentialValue] = Field(
        min_length=1, max_length=MAX_CREDENTIAL_FIELDS
    )
    #: What language this destination publishes in. Defaults to English, which
    #: is what every connection made before translations existed means.
    language: str = Field(default=languages.SOURCE_LANGUAGE, max_length=16)

    @field_validator("language")
    @classmethod
    def _supported(cls, value: str) -> str:
        """Refuse a language the translator cannot produce.

        Accepting one would set a destination to a language no translation can
        ever satisfy, so every publish falls back to English and reports a
        reason nobody reads. The failure belongs at the moment the user picks
        it. Normalised on the way in, so the column holds a base tag and the
        publish-path comparison is an equality rather than a parse.
        """
        code = languages.normalize(value)
        if code is None:
            raise ValueError(
                f"{value!r} is not a supported language. See GET /languages."
            )
        return code


class ConnectionOut(BaseModel):
    id: int
    platform: Platform
    status: ConnectionStatus
    display_name: str | None = None
    last_verified_at: datetime | None = None
    last_error: str | None = None
    #: The language this destination publishes in. Always present and never
    #: null: a connection that has never been told publishes English.
    language: str = languages.SOURCE_LANGUAGE

    model_config = {"from_attributes": True}


class CredentialFieldOut(BaseModel):
    key: str
    label: str
    help_text: str = ""
    secret: bool = True
    required: bool = True


class PlatformCapability(BaseModel):
    """What Herald can do with a platform, and what it needs to do it."""

    platform: str
    display_name: str
    implemented: bool
    supports_metrics: bool
    caveat: str = ""
    credential_fields: list[CredentialFieldOut] = []
    #: Filled in per-user by the settings endpoint.
    connection: ConnectionOut | None = None


class DependencyOut(BaseModel):
    """One backing service the health check probed."""

    #: "ok" | "unavailable" | "disabled" (the dependency is switched off by
    #: configuration, so there is nothing to be unavailable).
    status: str
    #: Whether ``status != "ok"`` is enough to fail the whole health check.
    required: bool = True
    #: Truncated cause when unavailable; empty otherwise.
    detail: str = ""


class RootOut(BaseModel):
    """What ``GET /`` answers: where the API is, and nothing about who asked.

    ``docs`` is present only where the schema is actually served — see
    :func:`app.main.docs_enabled`. Advertising a route that 404s is worse than
    saying nothing.
    """

    app: str
    health: str
    docs: str | None = None


class HealthOut(BaseModel):
    """Public liveness probe — no operational details leak."""

    status: str
    database: DependencyOut = DependencyOut(status="unknown")
    redis: DependencyOut = DependencyOut(status="unknown")


class HealthDetailOut(HealthOut):
    """Authenticated detail view — answers "why isn't X working?"."""

    #: Whether anything is draining the task queue. Only here, never on the
    #: public probe — see :func:`app.routers.misc._check_workers`. Never
    #: required: a dead worker does not stop the API answering, and failing
    #: this check would pull the site out of Caddy's rotation over a background
    #: outage.
    workers: DependencyOut = DependencyOut(status="unknown", required=False)
    llm_providers: list[str] = []
    llm_breakers_open: dict = {}
    #: The publishing breakers, keyed ``platform:user_id``. Beside the LLM ones
    #: because they answer the same question from the other end: "why has
    #: nothing published?" has two shapes, and "the route to Dev.to for this
    #: account is being skipped for another four minutes" is one of them.
    publish_breakers_open: dict = {}
    github_configured: bool = False
    credential_encryption: bool = False
    implemented_platforms: list[str] = []
