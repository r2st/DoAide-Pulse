"""Content, publication and calendar schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.content import ContentStatus, ContentType
from app.models.publication import Platform, PublicationStatus


class GenerateRequest(BaseModel):
    """Ask the engine for a new draft."""

    project_id: int
    content_type: ContentType = ContentType.FEATURE_SPOTLIGHT
    #: Free text steering the piece — "focus on the Celery retry logic".
    instructions: str = Field(default="", max_length=2000)
    #: Pull the latest commits/releases from GitHub first. Off by default
    #: because it costs a round trip and only helps for release-driven pieces.
    include_repo_activity: bool = False


class ContentCreate(BaseModel):
    """Write a piece by hand, without the generator."""

    project_id: int
    content_type: ContentType = ContentType.FEATURE_SPOTLIGHT
    title: str = Field(min_length=1, max_length=300)
    body_markdown: str = ""
    excerpt: str = ""
    meta_description: str = ""
    keywords: list[str] = []
    tags: list[str] = []
    canonical_url: str | None = None


class ContentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body_markdown: str | None = None
    excerpt: str | None = None
    meta_description: str | None = None
    keywords: list[str] | None = None
    tags: list[str] | None = None
    canonical_url: str | None = None
    content_type: ContentType | None = None
    status: ContentStatus | None = None
    scheduled_for: datetime | None = None


class PublicationOut(BaseModel):
    id: int
    content_id: int
    platform: Platform
    status: PublicationStatus
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    external_url: str | None = None
    as_draft: bool = False
    attempts: int = 0
    error: str | None = None

    model_config = {"from_attributes": True}


class SeoIssueOut(BaseModel):
    level: str
    field: str
    message: str


class ContentOut(BaseModel):
    id: int
    project_id: int
    project_name: str | None = None
    content_type: ContentType
    status: ContentStatus
    title: str
    slug: str
    excerpt: str
    meta_description: str
    keywords: list[str] = []
    tags: list[str] = []
    canonical_url: str | None = None
    confidence: float | None = None
    generated_by_provider: str | None = None
    generated_by_model: str | None = None
    source: dict = {}
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    word_count: int = 0
    read_minutes: int = 1
    publications: list[PublicationOut] = []

    model_config = {"from_attributes": True}


class ContentDetail(ContentOut):
    """The list shape plus the body — the editor's payload.

    Kept separate so a list of 200 pieces doesn't ship 200 full post bodies.
    """

    body_markdown: str = ""
    seo_issues: list[SeoIssueOut] = []


class PublishRequestIn(BaseModel):
    """Queue a piece for one or more platforms."""

    platforms: list[Platform] = Field(min_length=1)
    #: ``None`` publishes as soon as a worker picks it up.
    scheduled_for: datetime | None = None
    #: Create it as a draft on the platform rather than going live.
    as_draft: bool = False


class ScheduleUpdate(BaseModel):
    """Drag-and-drop on the calendar lands here."""

    scheduled_for: datetime | None = None
    #: Optional: move only this publication rather than the whole piece.
    publication_id: int | None = None


class CalendarEntry(BaseModel):
    """One item on the calendar — scheduled or already published."""

    content_id: int
    publication_id: int | None = None
    title: str
    project_id: int
    project_name: str
    content_type: ContentType
    platform: Platform | None = None
    status: str
    when: datetime
    #: False for a published item, which must not be draggable.
    movable: bool = True


class CalendarOut(BaseModel):
    entries: list[CalendarEntry] = []
    #: Cadence guidance for the platforms this user has connected.
    cadence: list[dict] = []
    #: Empty slots the calendar offers as "schedule something here".
    suggested_slots: list[datetime] = []
