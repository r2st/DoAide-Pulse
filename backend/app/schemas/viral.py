"""Schemas for viral / public-facing features."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PublicArticleOut(BaseModel):
    """A published article visible to anyone with the link."""

    title: str
    slug: str
    body_markdown: str
    excerpt: str
    cover_image_url: str | None = None
    meta_description: str = ""
    tags: list[str] = []
    word_count: int = 0
    read_minutes: int = 1
    project_name: str | None = None
    published_at: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    source: str = Field(default="embed", max_length=100)


class SubscribeOut(BaseModel):
    ok: bool = True


class ContentIdeasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    niche: str = Field(min_length=2, max_length=200)
    count: int = Field(default=5, ge=1, le=10)


class ContentIdeasOut(BaseModel):
    ideas: list[ContentIdeaItem]
    niche: str


class ContentIdeaItem(BaseModel):
    title: str
    hook: str
    content_type: str


# Rebuild ContentIdeasOut now that ContentIdeaItem is defined.
ContentIdeasOut.model_rebuild()
