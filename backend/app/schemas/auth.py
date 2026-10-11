"""Auth-related schemas."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        return name or None
    #: Required when ``REGISTRATION_INVITE_TOKEN`` is configured; ignored
    #: otherwise. Registration is closed by default — see app.routers.auth.
    invite_token: str | None = Field(default=None, max_length=256)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)
    #: Same floor as registration — a reset must not be a way around it.
    new_password: str = Field(min_length=8, max_length=128)


class MessageOut(BaseModel):
    """A human-readable result for endpoints with nothing else to return."""

    detail: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None = None
    is_active: bool
    #: Whether the Monday performance summary goes out. See app.services.digest.
    weekly_digest_enabled: bool = True
    connected_platforms: list[str] = []

    model_config = {"from_attributes": True}


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=200)
    weekly_digest_enabled: bool | None = None

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        return name or None
