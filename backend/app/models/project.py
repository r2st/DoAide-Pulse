"""A project Pulse writes about.

One row per thing the developer wants promoted — DoAide Jobs, GoSumo, Pulse
itself. Everything the content engine needs to write in the project's voice
lives here, alongside the repo watermarks the autopilot uses to notice that
something new has shipped.
"""
from __future__ import annotations

import re

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime, timedelta  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin, as_aware, utcnow

# Imported at runtime for the same reason as ``datetime`` above: it appears in a
# ``Mapped[...]`` annotation. Safe despite the apparent cycle — ``publication``
# imports ``project`` only under TYPE_CHECKING.
from app.models.publication import Platform

if TYPE_CHECKING:
    from app.models.api_key import ApiKey
    from app.models.content import Content
    from app.models.trigger import Trigger
    from app.models.user import User


#: How much of a release tag the watermark column holds. Git allows a ref name
#: of up to 255 bytes and GitHub hands the whole thing back, so the value that
#: lands here is bounded by us or by nothing. Named rather than repeated
#: because :mod:`app.services.github_client` has to apply the same number at
#: the point it parses a release — see the comment there for why truncating on
#: the way *in* is the only version of this that works.
RELEASE_TAG_MAX_LENGTH = 120

#: How much of a commit sha the watermark column holds. Forty hex characters is
#: a SHA-1 object name, which is every sha GitHub returns today.
#:
#: Named for the same reason as the tag above, and it is the same bug waiting in
#: the same place: the sha is written straight out of a response body into a
#: fixed-width column, from three call sites, and none of them looked at its
#: length. Anything wider is ``StringDataRightTruncation`` on PostgreSQL from
#: the commit that stores the watermark — a 500 on the manual scan, and in the
#: autopilot a watermark that never advances, so the same commits are re-read
#: and written about again on every scan for ever. Git's own SHA-256 transition
#: doubles this to 64; that is not a thing GitHub serves yet, and it is exactly
#: the sort of thing that arrives without asking.
COMMIT_SHA_MAX_LENGTH = 40


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


def scan_due(
    last_scanned_at: datetime | None,
    min_interval_hours: int | None,
    now: datetime | None = None,
) -> bool:
    """Whether a project on this interval may be scanned again yet.

    ``True`` unless *min_interval_hours* is set and the last completed scan is
    more recent than that. A project that has never scanned is always due — the
    interval is a gap between scans, and there is no first scan to measure a gap
    from.

    A free function over two values rather than only a method on
    :class:`Project`, because the beat sweep tests the whole fleet and selects
    three columns rather than whole rows to do it — see
    :func:`app.tasks.autopilot_tasks.scan_all_projects`. Building a throwaway
    ``Project`` per row to reach a method would put transient instances in an
    open session for no reason, and widening the select to whole entities to
    avoid that would pull every project's description on every tick.
    :meth:`Project.scan_due` delegates here so the two cannot drift.

    Takes *now* rather than reading the clock, because the sweep tests one
    clock against every project: a pass that read the time per project would
    let a slow pass drift over the boundary halfway through and scan the back
    half of the fleet a tick early.

    Measures from ``last_scanned_at`` — when the repo was last *looked at*, not
    when a piece was last written. A scan that finds nothing new still counts,
    which is the intended reading: the interval bounds how often Pulse goes
    and asks, and asking is what spends the GitHub quota. What comes back is
    what ``autopilot_commit_threshold`` and ``autopilot_daily_content_limit``
    are for.
    """
    hours = min_interval_hours or 0
    if hours <= 0 or last_scanned_at is None:
        return True
    return as_aware(last_scanned_at) <= (now or utcnow()) - timedelta(hours=hours)


#: ``owner/repo`` out of any of the GitHub URL shapes people actually paste.
_REPO_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$", re.I
)


def repo_full_name(repo_url: str | None) -> str | None:
    """``owner/repo`` for the GitHub API, or ``None`` if *repo_url* is not one.

    A free function over the one column rather than only a method on
    :class:`Project`, for the same reason as :func:`scan_due` above: the beat
    sweep in :func:`app.tasks.autopilot_tasks.scan_all_projects` tests the whole
    fleet from a few selected columns, and it needs this answer per row. Before
    it could ask, the sweep's only filter was ``repo_url IS NOT NULL`` — which a
    GitLab URL passes. Such a project was dispatched, turned away by
    ``scan_project`` with ``no_repo`` before it recorded a scan, and so came back
    ``last_scanned_at IS NULL`` and due again on the very next tick, forever,
    with its own ``autopilot_min_interval_hours`` never once applying because an
    interval is measured from a scan that never happened.

    :attr:`Project.repo_full_name` delegates here so the two cannot drift.
    """
    if not repo_url:
        return None
    match = _REPO_RE.search(repo_url.strip())
    if not match:
        return None
    return f"{match['owner']}/{match['repo']}"


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
    #: Let Pulse swap in the best-performing past headline on its own. Off by
    #: default, and deliberately: a title changing under the author without
    #: their say-so is startling, and the measurement carries a known bias
    #: toward whichever headline was live at launch (see
    #: ``app.services.headlines.pick_winner``). Opting in is accepting that
    #: trade in exchange for never having to check.
    auto_headline_winner: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    #: Fire ``content.engagement_threshold`` once a piece from this project
    #: passes this many total interactions. ``0`` is off, and is the default:
    #: an alert nobody chose a number for is an alert that fires at the wrong
    #: time and gets muted, taking the ones that mattered with it.
    #:
    #: Per project rather than per account because the number that means
    #: "this one is doing unusually well" is a property of the audience, and a
    #: side project's fifty is a flagship's five hundred.
    #:
    #: Counts interactions, not views — the four counters
    #: :attr:`app.models.metrics.ContentMetric.engagement` adds. Views are the
    #: number platforms disagree about most (some count an impression, some a
    #: scroll), so a threshold on them would mean something different per
    #: platform and nothing across them.
    engagement_threshold: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
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
    #: Hours this project must go between autopilot scans. ``0`` means "every
    #: sweep", which is what every project did before this column existed.
    #:
    #: The scan interval was a single deployment-wide number
    #: (``autopilot_scan_interval_seconds``), so "write about this repo at most
    #: twice a week" could only be expressed by slowing every project on the
    #: box down to that rate. The two knobs beside this one do not cover it
    #: either: ``autopilot_commit_threshold`` gates on *how much* has shipped
    #: rather than how long it has been, and ``autopilot_daily_content_limit``
    #: is a ceiling on a day, so a repo that clears the threshold every hour
    #: still writes every day it possibly can. This is the one that says how
    #: often, and it is per project because a documentation repo and a product
    #: repo want different answers on the same install.
    #:
    #: Read only by the beat sweep — see
    #: :func:`app.tasks.autopilot_tasks.scan_all_projects`. A human pressing
    #: ``POST /projects/{id}/scan`` is deliberately not held: they are asking
    #: for this repo to be looked at now, and they know something the interval
    #: does not. Same reasoning as :func:`app.services.publishing_service.retry_hold`
    #: exempting nothing but the automated path.
    autopilot_min_interval_hours: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    #: Watermarks: what the monitor had already seen last time it looked. A
    #: change against these is the trigger, so a first scan of an old repo
    #: records where it is rather than writing about two years of history.
    last_seen_commit_sha: Mapped[str | None] = mapped_column(
        String(COMMIT_SHA_MAX_LENGTH)
    )
    last_seen_release_tag: Mapped[str | None] = mapped_column(
        String(RELEASE_TAG_MAX_LENGTH)
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How long the last completed scan of this repo took, end to end, including
    #: the GitHub round-trips. ``None`` until one finishes.
    #:
    #: Pulse has no metrics backend, so "the autopilot got slow" was a thing
    #: somebody noticed in the UI. A scan is bounded by a 180-second soft time
    #: limit and does two or three HTTP calls to a rate-limited API, so it is
    #: both the slowest recurring thing on the box and the one whose slowdown is
    #: least visible — a scan that starts timing out simply stops producing
    #: content, which looks like a quiet week.
    last_scan_duration_ms: Mapped[int | None] = mapped_column(Integer)
    #: Completed scans of this repo, ever. With ``created_at`` this is the
    #: scan *frequency*, which is the number that says whether the beat schedule
    #: is doing what it was configured to do — a project scanned four times in a
    #: fortnight is a project whose sweep is not running, and nothing else
    #: reports that.
    #:
    #: Counted rather than derived from a table of scan rows: the value is read
    #: by one endpoint and the alternative is a table that grows forever to
    #: answer a question a counter answers.
    scan_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="projects")
    content: Mapped[list[Content]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    #: Everything that can make Pulse write about this project. The repo
    #: watermark columns above are the pre-trigger way of expressing one of
    #: these; a ``github`` trigger keeps its own watermark in ``Trigger.state``
    #: and takes the project out of the legacy scan.
    triggers: Mapped[list[Trigger]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    #: Machine credentials scoped to this project. Cascaded so deleting a
    #: project cannot leave a live key naming a row that is gone.
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    def record_scan(self, *, duration_ms: int) -> None:
        """Note that a scan of this repo finished, and how long it took.

        Does not commit — the caller is mid-transaction with the watermark it is
        about to write beside this.

        On the model rather than in either caller because there are two of them
        and they live on opposite sides of the app: the beat sweep in
        :mod:`app.tasks.autopilot_tasks` and the hand-run ``POST
        /projects/{id}/scan``. A router importing the task module to share a
        helper would pull Celery onto the request path for three lines of
        arithmetic.

        ``scan_count`` is incremented from the loaded value rather than with a
        SQL ``+ 1``: one project is scanned by one task at a time — the sweep
        dispatches by id and does not fan a project out to two workers — so
        there is no race for an atomic update to win.

        Not called on the rate-limited path. Nothing was read there,
        ``last_scanned_at`` deliberately stays where it was so the next sweep
        still treats the project as due, and counting it would inflate a scan
        frequency with attempts that never happened.
        """
        self.last_scanned_at = utcnow()
        # Negative is not a duration. The value arrives from a caller's
        # subtraction, and a stored negative would drag an average in the one
        # direction nobody sanity-checks.
        self.last_scan_duration_ms = max(0, int(duration_ms))
        self.scan_count = (self.scan_count or 0) + 1

    @property
    def campaign(self) -> str:
        """The ``utm_campaign`` value for this project's outbound links."""
        return (self.utm_campaign or "").strip() or self.slug

    @property
    def repo_full_name(self) -> str | None:
        """``owner/repo`` for the GitHub API, or ``None`` if not a GitHub repo.

        See :func:`repo_full_name`, which this delegates to.
        """
        return repo_full_name(self.repo_url)

    def scan_due(self, now: datetime | None = None) -> bool:
        """Whether the beat sweep may scan this project yet. See :func:`scan_due`."""
        return scan_due(self.last_scanned_at, self.autopilot_min_interval_hours, now)

    @property
    def autopilot_blocked_reason(self) -> str | None:
        """Why this project's autopilot can never fire, or ``None`` if it can.

        The autopilot sweep selects on ``repo_url IS NOT NULL`` and the scan
        itself needs a *GitHub* URL it can parse, so a project can sit at
        ``auto`` — badge lit, switch on, nothing wrong on the page — and be
        structurally incapable of ever producing a post. Nothing said so, and
        the only symptom was an absence: no content, no error, no log line.

        Reported rather than corrected on purpose. A project genuinely may not
        have a repo, and the answer then is a trigger or a manual piece, not a
        URL invented to satisfy the scan.

        Reads ``self.triggers``: any active trigger — an RSS feed, a schedule, an
        inbound webhook — drives the project through the same pipeline without a
        repo, so a project with one is not blocked. Callers that serialize more
        than one project should eager-load the relationship.

        **Two ways to be blocked, not one.** The check above is about *starting*
        a piece, and for a while it was the whole of this property — so a project
        on ``auto`` whose destinations it could never publish to reported nothing
        wrong. That project writes: the autopilot fires, the piece is generated,
        and then ``publishable_destinations`` drops every destination and
        ``generate_and_route`` parks the piece in review because there is nowhere
        to send it. From the outside that is indistinguishable from the quality
        gates doing their job, and the switch still says ``auto``. It is the
        failure this project had in production — an autopilot destination the
        owner had never connected — and the badge that exists to explain a silent
        autopilot said the autopilot was fine.

        The finishing check applies only to ``auto``. On ``draft`` the pieces are
        *meant* to stop for a human, so having no destination is the setting
        rather than a fault.

        Reads ``self.user.connected_platforms`` on that path, which is one extra
        pair of loads per *account* rather than per project — every project in a
        listing has the same owner. Eager-load ``Project.user`` alongside the
        triggers if the identity map will not already hold it.
        """
        mode = (
            self.autopilot_mode
            if isinstance(self.autopilot_mode, AutopilotMode)
            else AutopilotMode(self.autopilot_mode)
        )
        if mode == AutopilotMode.OFF:
            return None

        return self._cannot_start() or self._cannot_finish(mode)

    def _cannot_start(self) -> str | None:
        """Why nothing will ever *write* a piece for this project."""
        if any(trigger.is_active for trigger in self.triggers):
            return None
        if not self.repo_url:
            return (
                "No repository is linked and no trigger is set, so nothing can "
                "start a piece."
            )
        if self.repo_full_name is None:
            return (
                "The repository URL is not a GitHub repo, which is the only "
                "kind the scan can read."
            )
        return None

    def _cannot_finish(self, mode: AutopilotMode) -> str | None:
        """Why a piece this project writes will never *publish* itself."""
        if mode != AutopilotMode.AUTO:
            return None

        # Imported here rather than at module scope: the pipeline imports the
        # publisher registry, which imports this module.
        from app.services.content_pipeline import publishable_destinations

        destinations = publishable_destinations(self)
        if destinations.usable:
            return None
        if destinations.unconnected:
            return (
                "Autopilot publishes to "
                + ", ".join(destinations.unconnected)
                + ", which this account is not connected to, so every piece "
                "stops for review. Connect them in Settings."
            )
        if not self.autopilot_platforms:
            return (
                "Autopilot is set to publish on its own but names no platforms, "
                "so every piece stops for review."
            )
        # Named platforms that survived neither the enum nor the registry:
        # a value from before the schema validators, or an adapter that is not
        # finished. `publishable_destinations` logs which.
        return (
            "None of the platforms autopilot names can be published to, so "
            "every piece stops for review."
        )

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
