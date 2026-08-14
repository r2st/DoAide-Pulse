"""A generated piece of content, and the states it moves through.

Content is stored once, in Markdown, and adapted per platform at publish time
(see ``app.services.publishers``). That way a post that goes to Dev.to and
LinkedIn is genuinely the same piece rather than two divergent copies.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.preview_link import PreviewLink
    from app.models.project import Project
    from app.models.publication import Publication


#: Column widths that something outside this module has to agree with.
#:
#: They live here, next to the ``mapped_column`` that uses them, because the
#: schema layer's ``max_length`` is not an opinion about how long a description
#: ought to be — it is a claim about what the column can hold, and a claim wider
#: than the truth is a 500 rather than a lenient API. PostgreSQL answers an
#: over-long INSERT with ``StringDataRightTruncation``, which is not
#: ``IntegrityError``, so nothing in the tree catches it and the caller gets
#: "Internal server error" for a request the API had already validated. SQLite
#: ignores VARCHAR lengths altogether, which is exactly why a suite at 100%
#: coverage ran green over it for both fields below.
#:
#: Importing the name rather than repeating the number is what keeps the two
#: layers honest; ``tests/test_schema_caps_fit_their_columns.py`` pins the rest
#: of the tree, where the numbers are still written twice.
TITLE_MAX_LENGTH = 300
#: Wider than the title it is derived from, so the collision suffix that
#: ``unique_content_slug`` appends has somewhere to go.
SLUG_MAX_LENGTH = 320
META_DESCRIPTION_MAX_LENGTH = 320
#: ``body_markdown`` is ``Text``, so this one is not a column width — it is the
#: ceiling ``ContentCreate`` has always applied, named here so the paths that
#: build a body *without* going through that schema apply the same one. A body
#: is written once and then read by every adapter, every SEO audit and every
#: render of the editor, so "unbounded because the column allows it" is a
#: promise the rest of the system cannot keep.
BODY_MARKDOWN_MAX_LENGTH = 200_000
#: Deliberately narrower than ``Publication.external_url`` (700), which is one
#: of the things written into it — see
#: ``app.services.publishing_service._adopt_canonical``.
CANONICAL_URL_MAX_LENGTH = 500
#: The longest one entry in ``tags`` may be.
#:
#: ``tags`` and ``keywords`` are JSON columns, so the count cap on the schema
#: field was the *only* bound either list had: thirty entries of any size each.
#: A keyword at least met :data:`app.services.seo.KEYWORD_MAX_LENGTH` on the way
#: through — ``normalize_keywords`` drops the over-long ones — but a tag met
#: nothing at all between the request body and the adapter that posts it, and
#: ``normalize_tags`` strips characters rather than length.
#:
#: Same number as the keyword cap on purpose. Both are short labels, both are
#: shown in the same editor panel, and a tag allowed to be longer than a keyword
#: would be a distinction with no reason behind it. Per-platform truncation
#: stays where it belongs, in ``publishers.formatting``.
TAG_MAX_LENGTH = 100


def word_count_of(body_markdown: str) -> int:
    """Words in a body. The definition :attr:`Content.word_count` uses."""
    return len(body_markdown.split())


def read_minutes_of(body_markdown: str) -> int:
    """Reading time at ~220wpm, floored at one minute.

    Free functions as well as properties because both quantities are derived
    from the body and nothing else, and the aggregations in
    :mod:`app.services.analytics_service` want them over thousands of rows.
    Reading a ``Content`` entity per row to reach the property loads every
    column plus, through ``lazy="selectin"``, every publication attached to it —
    to arrive at a number that only ever needed ``body_markdown``. The callers
    select that one column and apply this; the property below is the same
    function, so the two cannot drift.
    """
    return max(1, round(word_count_of(body_markdown) / 220))


class ContentType(str, Enum):
    """What kind of piece this is. Drives the prompt and the target length.

    The first five are articles. The last two are not — see
    :mod:`app.services.formats`, which maps a type to the *shape* it produces
    and is what stops a thread from being written as a blog post with the
    headings taken out.
    """

    TUTORIAL = "tutorial"
    ANNOUNCEMENT = "announcement"
    FEATURE_SPOTLIGHT = "feature_spotlight"
    COMPARISON = "comparison"
    HOW_TO = "how_to"
    SOCIAL_THREAD = "social_thread"
    CHANGELOG = "changelog"

    @property
    def label(self) -> str:
        """Title-cased name for the UI. ``feature_spotlight`` → Feature Spotlight."""
        return self.value.replace("_", " ").title()


class ContentStatus(str, Enum):
    """draft → review → approved → published, plus the two exits.

    ``review`` is where the autopilot parks anything it isn't confident about;
    ``approved`` means a human said yes but it hasn't gone out yet (either it's
    scheduled, or the publish is still in flight).
    """

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"


#: Rough target length per type, in words. The generator passes this to the
#: model and the token budget is derived from it — a tutorial that comes back
#: at tweet length is a failure, and so is an announcement that runs to 2000
#: words nobody reads.
TARGET_WORDS: dict[ContentType, int] = {
    ContentType.TUTORIAL: 1200,
    ContentType.ANNOUNCEMENT: 450,
    ContentType.FEATURE_SPOTLIGHT: 700,
    ContentType.COMPARISON: 1000,
    ContentType.HOW_TO: 900,
    # Not articles: these are a token budget rather than a target. A thread of
    # eight 280-character posts is about 300 words, and a changelog is as long
    # as the release was — the number here only has to be generous enough that
    # the envelope is never cut off mid-JSON.
    ContentType.SOCIAL_THREAD: 320,
    ContentType.CHANGELOG: 400,
}


class Content(Base, TimestampMixin):
    __tablename__ = "content"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_content_project_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )

    content_type: Mapped[ContentType] = mapped_column(
        SAEnum(ContentType, native_enum=False, length=30), nullable=False
    )
    status: Mapped[ContentStatus] = mapped_column(
        SAEnum(ContentStatus, native_enum=False, length=20),
        default=ContentStatus.DRAFT,
        index=True,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(SLUG_MAX_LENGTH), index=True, nullable=False
    )
    #: The canonical body. Markdown — every adapter converts *from* this.
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: One- or two-sentence summary; doubles as the social blurb.
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- SEO ----
    meta_description: Mapped[str] = mapped_column(
        String(META_DESCRIPTION_MAX_LENGTH), default="", nullable=False
    )
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    #: The single keyword this piece is optimised for. Drives the SEO audit
    #: score (keyword density, first-paragraph presence, subheading inclusion).
    #: Populated automatically from the first project keyword during generation.
    focus_keyword: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    #: Tags as the blogging platforms mean them (Dev.to caps at 4, Medium at 5).
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    #: Set when the piece is published somewhere first and syndicated after —
    #: the adapters send it as rel=canonical so the copies don't compete.
    canonical_url: Mapped[str | None] = mapped_column(String(CANONICAL_URL_MAX_LENGTH))

    # ---- Media ----
    #: The image the platform shows beside this post in its feed, and the one
    #: LinkedIn and Twitter use for the link preview. An absolute URL rather than
    #: an upload: every destination Herald publishes to takes a URL and fetches
    #: it itself, so hosting the bytes would add a storage story to the deploy
    #: for no gain on the platform side.
    cover_image_url: Mapped[str | None] = mapped_column(String(700))

    # ---- Provenance ----
    #: Which provider/model actually wrote it, for the "what works" analysis.
    generated_by_provider: Mapped[str | None] = mapped_column(String(40))
    generated_by_model: Mapped[str | None] = mapped_column(String(120))
    #: The model's own read on whether this is ready to go out unreviewed.
    #: Drives the auto-publish gate; ``None`` for anything a human wrote.
    confidence: Mapped[float | None] = mapped_column(Float)
    #: What prompted it — {"kind": "release", "tag": "v1.2.0", ...} or
    #: {"kind": "manual", "user_id": 1}. Kept for the analytics breakdown.
    source: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # ---- Scheduling ----
    #: When this should go out. ``None`` means "on approval, immediately".
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- Headline testing ----
    #: Past titles and the [started_at, ended_at) window each was live —
    #: {"title": ..., "started_at": iso, "ended_at": iso}. The *current*
    #: title's own window is not stored here; it is derived at read time from
    #: the last entry's ``ended_at`` (or ``created_at`` if this is empty) so a
    #: piece that never had its headline changed costs nothing. See
    #: ``app.services.headlines``.
    headline_history: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    project: Mapped[Project] = relationship(back_populates="content")
    publications: Mapped[list[Publication]] = relationship(
        back_populates="content", cascade="all, delete-orphan", lazy="selectin"
    )
    preview_links: Mapped[list[PreviewLink]] = relationship(
        back_populates="content", cascade="all, delete-orphan"
    )

    @property
    def word_count(self) -> int:
        """Words in the body. Whitespace-split, so markdown markers count."""
        return word_count_of(self.body_markdown)

    @property
    def read_minutes(self) -> int:
        """Reading time at ~220wpm, floored at one minute."""
        return read_minutes_of(self.body_markdown)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Content id={self.id} title={self.title!r} status={self.status}>"


class ContentIdea(Base, TimestampMixin):
    """A subject worth writing about that nobody has written yet.

    The repo monitor produces these; the calendar's "suggested" slots consume
    them. Kept separate from ``Content`` so an unwritten idea never shows up in
    a content list, an export, or a word count.
    """

    __tablename__ = "content_ideas"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_type: Mapped[ContentType] = mapped_column(
        SAEnum(ContentType, native_enum=False, length=30), nullable=False
    )
    headline: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    #: Set once the idea has been turned into a piece, so it stops being
    #: suggested. Not a FK — an idea outlives the draft that a user deletes.
    used_content_id: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ContentIdea id={self.id} headline={self.headline!r}>"


def unique_content_slug(db: Session, project_id: int, title: str) -> str:
    """A slug unique within this project's content.

    Used by both the content router and the autopilot task — kept here so the
    query and the model live in the same module. The database-level unique
    constraint is the real guard; this avoids the common case.

    The base is clipped so that the disambiguating suffix still fits inside
    ``slug``'s column. That is belt-and-braces where the title came through a
    schema — ``TITLE_MAX_LENGTH`` already leaves twenty characters of room — but
    every caller here passes a title from somewhere, and this is the one place
    all of them meet. It matters more than the usual column overflow: ``slug``
    carries the ``uq_content_project_slug`` unique index, and PostgreSQL refuses
    an index entry over 2704 bytes with a different error than the one a plain
    over-long value raises, so the failure would not even look like the same
    bug.
    """
    from app.models.project import slugify

    #: Room for "-2" through the "-abc123" the collision retry appends.
    room = SLUG_MAX_LENGTH - 8
    base = slugify(title)[:room].strip("-") or "untitled"
    candidate, suffix = base, 2
    while db.scalar(
        select(Content.id).where(
            Content.project_id == project_id, Content.slug == candidate
        )
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
