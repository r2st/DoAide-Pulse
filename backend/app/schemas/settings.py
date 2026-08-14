"""Platform-connection and preference schemas.

Credential *values* never appear in a response model. The only direction they
travel is inbound.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.platform_connection import ConnectionStatus
from app.models.publication import Platform
from app.schemas.limits import (
    MAX_CREDENTIAL_FIELDS,
    CredentialKey,
    CredentialValue,
)


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


class ConnectionOut(BaseModel):
    id: int
    platform: Platform
    status: ConnectionStatus
    display_name: str | None = None
    last_verified_at: datetime | None = None
    last_error: str | None = None

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

    #: "ok" | "unavailable".
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

    llm_providers: list[str] = []
    llm_breakers_open: dict = {}
    github_configured: bool = False
    credential_encryption: bool = False
    implemented_platforms: list[str] = []
