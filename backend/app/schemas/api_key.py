"""API key request and response models.

The token appears in exactly two responses — the one that mints it and the one
that rotates it — and in none other, which is why :class:`ApiKeyCreated` is a
separate model rather than :class:`ApiKeyOut` with an optional field. The same
argument :mod:`app.schemas.webhook` makes about signing secrets: a field that is
usually absent is a field that gets returned by accident eventually.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.api_key import ALL_SCOPES, ApiKeyScope
from app.models.content import ContentType
from app.services.api_keys import MAX_EXPIRY_DAYS, MAX_GRACE_HOURS


def _validate_scopes(scopes: list[ApiKeyScope]) -> list[ApiKeyScope]:
    if not scopes:
        raise ValueError(
            "Grant at least one scope. Available: "
            + ", ".join(s.value for s in ALL_SCOPES)
            + "."
        )
    # Deduplicated and ordered here as well as in the service, so the 422 a
    # client gets for a bad scope and the shape it gets back for a good one are
    # both decided at the edge.
    ordered = {s.value: s for s in scopes}
    return [s for s in ALL_SCOPES if s.value in ordered]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int
    name: str = Field(min_length=1, max_length=120)
    scopes: list[ApiKeyScope]
    #: ``None`` means "until revoked". See ``api_keys.expiry_from_days``.
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_EXPIRY_DAYS)

    _scopes = field_validator("scopes")(_validate_scopes)


class ApiKeyRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_hours: int = Field(default=0, ge=0, le=MAX_GRACE_HOURS)


class ApiKeyOut(BaseModel):
    id: int
    project_id: int
    name: str
    #: The lookup half of the token, in the clear. Enough to tell two keys
    #: apart in a log; not enough to use one.
    prefix: str
    scopes: list[str]
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    rotated_from_id: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyOut):
    """A freshly minted key, plus the token, shown once and never again."""

    token: str


class ApiKeyRotated(BaseModel):
    """The outcome of a rotation: both keys, and the new token."""

    key: ApiKeyCreated
    #: The key that was replaced, as it stands now — revoked, or dated to
    #: expire at the end of the grace window.
    replaced: ApiKeyOut


class ApiKeyScopeOut(BaseModel):
    """One scope and what it lets a key do, for the key-creation form."""

    scope: str
    description: str


class MachineIdentityOut(BaseModel):
    """Who a key is, answered to the key itself.

    The introspection response. A CI job calls this to check its credential is
    alive and carries what it thinks it carries — the machine equivalent of
    ``/auth/me``, and the one endpoint every key can reach regardless of scope.
    """

    key_id: int
    prefix: str
    name: str
    project_id: int
    project_slug: str
    scopes: list[str]
    expires_at: datetime | None = None


class MachineContentOut(BaseModel):
    """One piece, as a machine reading the project sees it.

    Narrower than :class:`app.schemas.content.ContentOut` on purpose: no body,
    no brief, no internal scoring. A key holder gets the shipping record, not
    the workspace.
    """

    id: int
    title: str
    slug: str
    content_type: str
    status: str
    word_count: int
    canonical_url: str | None = None
    published_at: datetime | None = None
    updated_at: datetime


class MachineIdeaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=3, max_length=300)
    rationale: str = Field(default="", max_length=2000)
    #: Defaults to ``changelog``, which is what the overwhelming majority of
    #: machine-filed ideas are: something shipped, and somebody should say so.
    content_type: ContentType = ContentType.CHANGELOG


class MachineIdeaOut(BaseModel):
    """The filed idea, with the id needed to find it in the queue."""

    id: int
    headline: str
    content_type: str
    #: ``open`` until a piece is written from it, then ``used``. Derived from
    #: ``ContentIdea.used_content_id`` rather than stored — there is no third
    #: state, and a column would be a second place for the truth to live.
    status: str
    created_at: datetime


class MachineAnalyticsOut(BaseModel):
    """The project's numbers, added up. What a status page renders."""

    project_id: int
    published_count: int
    total_views: int
    total_engagement: int
    #: Distinct platforms this project has a live publication on.
    platforms: list[str]


__all__ = [
    "ApiKeyCreate",
    "ApiKeyCreated",
    "ApiKeyOut",
    "ApiKeyRotate",
    "ApiKeyRotated",
    "ApiKeyScopeOut",
    "MachineAnalyticsOut",
    "MachineContentOut",
    "MachineIdeaCreate",
    "MachineIdeaOut",
    "MachineIdentityOut",
]
