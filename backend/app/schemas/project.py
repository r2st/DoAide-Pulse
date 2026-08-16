"""Project registry schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.project import AutopilotMode, Tone
from app.models.publication import Platform
from app.schemas.limits import Keyword, TechStackEntry


def _publishable(platform: Platform) -> Platform:
    """Refuse a platform whose adapter is not finished.

    The ``Platform`` enum is the vocabulary; the adapter registry is what says
    whether Herald can actually post somewhere (see
    :mod:`app.services.publishers`). ``POST /content/{id}/publish`` has always
    checked the registry and answered 400 with the list of destinations that
    work, but the two *standing* settings — the autopilot's destinations and the
    project's canonical platform — took any enum member.

    That gap was not cosmetic. An autopilot project pointed at an unfinished
    adapter takes the auto-publish branch in
    :func:`app.services.content_pipeline.generate_and_route`, so the piece is
    written, approved, queued, and terminally failed by
    :class:`~app.services.publishers.base.NotImplementedAdapter` — which drives
    the content row to ``failed`` rather than into the review queue. The result
    is a piece nobody reviews and nobody publishes, produced on every scan.

    Imported inside the function: the registry pulls in every adapter, and a
    schema module is imported at app start before the services are needed.
    """
    from app.services import publishers

    if not publishers.get_adapter(platform).implemented:
        working = ", ".join(p.value for p in publishers.implemented_platforms())
        raise ValueError(
            f"No finished adapter for {platform.value}. Publishing works for: "
            f"{working}."
        )
    return platform


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
    #: Hours between automated scans of this project. ``0`` = every sweep, which
    #: is the deployment-wide rate and the behaviour every project had before
    #: this field existed. Capped at 30 days: past that the interval is really
    #: "off", and ``autopilot_mode`` says that without a number to misread.
    autopilot_min_interval_hours: int = Field(default=0, ge=0, le=720)
    #: Set ``content.canonical_url`` from the first public URL a piece gets.
    auto_canonical: bool = True
    #: Which destination counts as the original. ``None`` = first to publish wins.
    canonical_platform: Platform | None = None
    #: Let Herald swap in the best-performing past headline unattended. Off by
    #: default — see ``Project.auto_headline_winner`` for why.
    auto_headline_winner: bool = False
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
    """Register a project.

    The platform validators live here and on :class:`ProjectUpdate` rather than
    on :class:`ProjectBase`, so they guard the two *inbound* shapes only.
    :class:`ProjectOut` also derives from the base, and a project that stored an
    unfinished platform before this check existed must still be readable —
    otherwise the one endpoint that would let the user fix it is the endpoint
    that 500s.

    The two list fields are redeclared here for exactly that reason. Their
    per-entry bounds are new, so rows written before them exist; putting the
    bound on the base would make reading one an error rather than a thing the
    user could go and shorten.
    """

    tech_stack: list[TechStackEntry] = Field(default=[], max_length=25)
    keywords: list[Keyword] = Field(default=[], max_length=25)

    @field_validator("autopilot_platforms")
    @classmethod
    def _publishable_platforms(cls, values: list[Platform]) -> list[Platform]:
        # Deduplicated as well as checked: queueing the same platform twice is
        # one row either way (the unique constraint on content+platform), so a
        # repeat in the config only ever misleads the settings page.
        return list(dict.fromkeys(_publishable(p) for p in values))

    @field_validator("canonical_platform")
    @classmethod
    def _publishable_canonical(cls, value: Platform | None) -> Platform | None:
        return _publishable(value) if value is not None else None


class ProjectUpdate(BaseModel):
    """Every field optional — PATCH semantics."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    repo_url: str | None = Field(default=None, max_length=500)
    live_url: str | None = Field(default=None, max_length=500)
    tech_stack: list[TechStackEntry] | None = Field(default=None, max_length=25)
    target_audience: str | None = Field(default=None, max_length=500)
    keywords: list[Keyword] | None = Field(default=None, max_length=25)
    tone: Tone | None = None
    is_active: bool | None = None
    autopilot_mode: AutopilotMode | None = None
    autopilot_platforms: list[Platform] | None = None
    autopilot_min_interval_hours: int | None = Field(default=None, ge=0, le=720)
    auto_canonical: bool | None = None
    canonical_platform: Platform | None = None
    auto_headline_winner: bool | None = None
    utm_enabled: bool | None = None
    utm_campaign: str | None = Field(default=None, max_length=120)

    @field_validator("autopilot_platforms")
    @classmethod
    def _publishable_platforms(
        cls, values: list[Platform] | None
    ) -> list[Platform] | None:
        if values is None:
            return None
        return list(dict.fromkeys(_publishable(p) for p in values))

    @field_validator("canonical_platform")
    @classmethod
    def _publishable_canonical(cls, value: Platform | None) -> Platform | None:
        return _publishable(value) if value is not None else None


class ProjectOut(ProjectBase):
    id: int
    slug: str
    repo_full_name: str | None = None
    #: Set when the autopilot is on but cannot possibly fire — see
    #: ``Project.autopilot_blocked_reason``. ``None`` when it is fine.
    autopilot_blocked_reason: str | None = None
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
