"""A project Herald writes about.

One row per thing the developer wants promoted — TalentPing, GoSumo, Herald
itself. Everything the content engine needs to write in the project's voice
lives here, alongside the repo watermarks the autopilot uses to notice that
something new has shipped.
"""
from __future__ import annotations

import re

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

# Imported at runtime for the same reason as ``datetime`` above: it appears in a
# ``Mapped[...]`` annotation. Safe despite the apparent cycle — ``publication``
# imports ``project`` only under TYPE_CHECKING.
from app.models.publication import Platform

if TYPE_CHECKING:
    from app.models.content import Content
    from app.models.user import User


class Tone(str, Enum):
    """How a project's content should read.

    Set per project rather than per piece: a project has a voice, and a
    technical deep-dive and a launch tweet about the same repo should still
    sound like they came from the same place.
    """

    TECHNICAL = "technical"
    CASUAL = "casual"
    MARKETING = "marketing"


class AutopilotMode(str, Enum):
    """What the repo monitor is allowed to do when it spots a change."""

    #: Notice changes, write nothing. The default for a new project.
    OFF = "off"
    #: Generate a draft and park it in the review queue.
    DRAFT = "draft"
    #: Generate, and publish without review when confidence clears the bar.
    AUTO = "auto"


def slugify(value: str) -> str:
    """URL-safe slug from a project or content title."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "untitled"


#: ``owner/repo`` out of any of the GitHub URL shapes people actually paste.
_REPO_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$", re.I
)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_project_user_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500))
    live_url: Mapped[str | None] = mapped_column(String(500))
    #: Free-form list of technologies — ["FastAPI", "React", "Postgres"].
    tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    target_audience: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Seed keywords the SEO pass builds on. Generated content adds to these.
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    tone: Mapped[Tone] = mapped_column(
        SAEnum(Tone, native_enum=False, length=20), default=Tone.TECHNICAL, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- Syndication ----
    #: When true, the first public URL a piece gets becomes its
    #: ``canonical_url``, and every platform published to afterwards is told
    #: about it. Off means the field stays whatever a human typed.
    auto_canonical: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: The destination that *owns* the canonical URL for this project — the
    #: original, of which everything else is a syndicated copy. ``None`` means
    #: no destination is privileged and whichever publishes first wins, which is
    #: right for a project with one real home and wrong for a project whose blog
    #: is a slow-to-build static site.
    canonical_platform: Mapped[Platform | None] = mapped_column(
        SAEnum(Platform, native_enum=False, length=30)
    )

    # ---- Headline testing ----
    #: Let Herald swap in the best-performing past headline on its own. Off by
    #: default, and deliberately: a title changing under the author without
    #: their say-so is startling, and the measurement carries a known bias
    #: toward whichever headline was live at launch (see
    #: ``app.services.headlines.pick_winner``). Opting in is accepting that
    #: trade in exchange for never having to check.
    auto_headline_winner: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # ---- Attribution ----
    #: Append UTM parameters to the links published posts point at, so the
    #: project's own analytics can tell which platform sent the visit. Never
    #: applied to ``rel=canonical`` — see ``app.services.utm``.
    utm_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: ``utm_campaign`` for this project's links. Blank means the project slug,
    #: which is the answer almost everybody wants and nobody wants to type.
    utm_campaign: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    # ---- Autopilot ----
    autopilot_mode: Mapped[AutopilotMode] = mapped_column(
        SAEnum(AutopilotMode, native_enum=False, length=20),
        default=AutopilotMode.OFF,
        nullable=False,
    )
    #: Platforms an autopilot piece is queued for. Empty means "draft only".
    autopilot_platforms: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    #: Watermarks: what the monitor had already seen last time it looked. A
    #: change against these is the trigger, so a first scan of an old repo
    #: records where it is rather than writing about two years of history.
    last_seen_commit_sha: Mapped[str | None] = mapped_column(String(40))
    last_seen_release_tag: Mapped[str | None] = mapped_column(String(120))
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="projects")
    content: Mapped[list[Content]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    @property
    def campaign(self) -> str:
        """The ``utm_campaign`` value for this project's outbound links."""
        return (self.utm_campaign or "").strip() or self.slug

    @property
    def repo_full_name(self) -> str | None:
        """``owner/repo`` for the GitHub API, or ``None`` if not a GitHub repo."""
        if not self.repo_url:
            return None
        match = _REPO_RE.search(self.repo_url.strip())
        if not match:
            return None
        return f"{match['owner']}/{match['repo']}"

    def brief(self) -> dict[str, Any]:
        """The project facts a prompt needs, in one dict."""
        return {
            "name": self.name,
            "description": self.description,
            "tech_stack": list(self.tech_stack or []),
            "target_audience": self.target_audience,
            "live_url": self.live_url,
            "repo_url": self.repo_url,
            "tone": self.tone.value if isinstance(self.tone, Tone) else str(self.tone),
            "keywords": list(self.keywords or []),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Project id={self.id} name={self.name!r}>"
