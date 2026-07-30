"""Auth-related schemas."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)
    #: Required when ``REGISTRATION_INVITE_TOKEN`` is configured; ignored
    #: otherwise. Registration is closed by default — see app.routers.auth.
    invite_token: str | None = Field(default=None, max_length=256)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
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
    connected_platforms: list[str] = []

    model_config = {"from_attributes": True}
