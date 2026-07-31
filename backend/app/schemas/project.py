"""Project registry schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.project import AutopilotMode, Tone
from app.models.publication import Platform


class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=5000)
    repo_url: str | None = Field(default=None, max_length=500)
    live_url: str | None = Field(default=None, max_length=500)
    tech_stack: list[str] = Field(default=[], max_length=25)
    target_audience: str = Field(default="", max_length=500)
    keywords: list[str] = Field(default=[], max_length=25)
    tone: Tone = Tone.TECHNICAL
    is_active: bool = True
    autopilot_mode: AutopilotMode = AutopilotMode.OFF
    autopilot_platforms: list[Platform] = []
    #: Set ``content.canonical_url`` from the first public URL a piece gets.
    auto_canonical: bool = True
    #: Which destination counts as the original. ``None`` = first to publish wins.
    canonical_platform: Platform | None = None
    #: Tag outbound links so the project's analytics can attribute the visit.
    utm_enabled: bool = True
    #: ``utm_campaign``. Blank falls back to the project slug.
    utm_campaign: str = Field(default="", max_length=120)

    @field_validator("tech_stack", "keywords")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        # Trim, drop blanks, dedupe case-insensitively but keep the user's
        # capitalisation — "FastAPI" should not become "fastapi" in the UI.
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            out.append(text)
        return out[:25]


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    """Every field optional — PATCH semantics."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    repo_url: str | None = Field(default=None, max_length=500)
    live_url: str | None = Field(default=None, max_length=500)
    tech_stack: list[str] | None = Field(default=None, max_length=25)
    target_audience: str | None = Field(default=None, max_length=500)
    keywords: list[str] | None = Field(default=None, max_length=25)
    tone: Tone | None = None
    is_active: bool | None = None
    autopilot_mode: AutopilotMode | None = None
    autopilot_platforms: list[Platform] | None = None
    auto_canonical: bool | None = None
    canonical_platform: Platform | None = None
    utm_enabled: bool | None = None
    utm_campaign: str | None = Field(default=None, max_length=120)


class ProjectOut(ProjectBase):
    id: int
    slug: str
    repo_full_name: str | None = None
    last_seen_commit_sha: str | None = None
    last_seen_release_tag: str | None = None
    last_scanned_at: datetime | None = None
    created_at: datetime
    #: Denormalized counters the list page would otherwise need N+1 queries for.
    content_count: int = 0
    published_count: int = 0

    model_config = {"from_attributes": True}


class RepoActivityOut(BaseModel):
    """What the GitHub scan found, as returned by the manual refresh."""

    full_name: str
    new_commit_count: int
    new_release_tag: str | None = None
    stars: int = 0
    description: str = ""
    topics: list[str] = []
    commits: list[str] = []


class IdeaOut(BaseModel):
    id: int | None = None
    project_id: int
    content_type: str
    headline: str
    rationale: str = ""

    model_config = {"from_attributes": True}
