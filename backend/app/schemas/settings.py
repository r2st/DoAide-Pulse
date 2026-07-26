"""Platform-connection and preference schemas.

Credential *values* never appear in a response model. The only direction they
travel is inbound.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.platform_connection import ConnectionStatus
from app.models.publication import Platform


class ConnectionCreate(BaseModel):
    platform: Platform
    #: Keys match the adapter's ``credential_fields``. Validated against them by
    #: the router, so a typo is rejected rather than silently stored.
    credentials: dict[str, str] = Field(min_length=1)


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


class HealthOut(BaseModel):
    status: str
    llm_providers: list[str] = []
    llm_breakers_open: dict = {}
    github_configured: bool = False
    credential_encryption: bool = False
    implemented_platforms: list[str] = []
