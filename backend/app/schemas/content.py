"""Content, publication and calendar schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.content import (
    BODY_MARKDOWN_MAX_LENGTH,
    CANONICAL_URL_MAX_LENGTH,
    META_DESCRIPTION_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ContentStatus,
    ContentType,
)
from app.models.project import Tone
from app.models.publication import Platform, PublicationStatus
from app.schemas.limits import Keyword, Tag
from app.services import inline_edit


def _absolute_image_url(value: str | None) -> str | None:
    """Accept an absolute http(s) URL, or nothing.

    Relative paths are refused rather than resolved: the platforms fetch this
    themselves from their own servers, so "/static/cover.png" is a broken image
    on every one of them and there is no base URL here that would fix it.
    """
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            "must be an absolute http(s) URL — the platforms fetch this image "
            "from their own servers"
        )
    return url


def unique_platforms(value: list[Platform] | None) -> list[Platform] | None:
    """Drop repeats from a platform list, keeping the caller's order.

    ``["devto", "devto"]`` is not a request to publish twice — there is nowhere
    for the second copy to go. One row per (content, platform) is a database
    constraint (``uq_publication_content_platform``), and
    :func:`app.services.publishing_service.queue` builds its "already queued"
    index once before the loop, so the repeat was not recognised as one and the
    INSERT failed the constraint: a 500 on a payload the API had already
    accepted as valid. Deduping here rather than in ``queue`` fixes every entry
    point at once, including the scheduling pass in
    ``POST /content/{id}/schedule``, which sizes its slot list from this list
    before any publication row is touched.
    """
    if value is None:
        return None
    seen: set[Platform] = set()
    out: list[Platform] = []
    for platform in value:
        if platform in seen:
            continue
        seen.add(platform)
        out.append(platform)
    return out


def _absolute_canonical_url(value: str | None) -> str | None:
    """Accept an absolute http(s) URL, or nothing.

    This value is sent verbatim as ``rel=canonical`` by every adapter that
    supports one, and a canonical that is not a resolvable absolute URL is worse
    than none: a relative path resolves against the *syndicating* platform's own
    host, so "/blog/post" on Dev.to points the crawler at dev.to. Anything that
    is not http(s) — ``javascript:``, ``data:`` — has no business in a link tag
    at all. The URL Herald adopts on its own already has to pass this check (see
    ``publishing_service._adopt_canonical``); a hand-typed one did not.
    """
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            "must be an absolute http(s) URL — it is sent to the platforms as "
            "rel=canonical, and anything else resolves against their host"
        )
    return url


#: The statuses a caller may set directly. ``published`` and ``failed`` are
#: derived from the piece's publications by
#: :func:`app.services.publishing_service._sync_content_status`, and setting
#: either by hand makes the content row disagree with what is actually live —
#: a piece counted as published in the analytics with nothing behind it, or one
#: marked failed while a publication is still in flight.
_SETTABLE_STATUSES = frozenset(
    {
        ContentStatus.DRAFT,
        ContentStatus.REVIEW,
        ContentStatus.APPROVED,
        ContentStatus.ARCHIVED,
    }
)


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
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    body_markdown: str = Field(default="", max_length=BODY_MARKDOWN_MAX_LENGTH)
    excerpt: str = Field(default="", max_length=1000)
    meta_description: str = Field(default="", max_length=META_DESCRIPTION_MAX_LENGTH)
    keywords: list[Keyword] = Field(default=[], max_length=30)
    tags: list[Tag] = Field(default=[], max_length=30)
    canonical_url: str | None = Field(default=None, max_length=CANONICAL_URL_MAX_LENGTH)
    cover_image_url: str | None = Field(default=None, max_length=700)
    #: The primary SEO keyword. Defaults to the first keyword when omitted.
    focus_keyword: str = Field(default="", max_length=100)
    #: A caller's own stable identifier for this piece, stored on ``source``.
    #:
    #: Exists for scripted callers that have to find their own content again on
    #: the next run. Without one the only handle is the title, and a title is
    #: the single field an editing pass is most likely to change — so a
    #: headline edit orphans the old row and the next sync creates a duplicate
    #: instead of updating it. ``ContentOut`` already returns ``source``, so a
    #: caller that sets this can match on it with no extra endpoint.
    campaign_key: str | None = Field(default=None, max_length=200)

    _check_cover = field_validator("cover_image_url")(_absolute_image_url)
    _check_canonical = field_validator("canonical_url")(_absolute_canonical_url)


class ContentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH)
    body_markdown: str | None = Field(default=None, max_length=BODY_MARKDOWN_MAX_LENGTH)
    excerpt: str | None = Field(default=None, max_length=1000)
    meta_description: str | None = Field(
        default=None, max_length=META_DESCRIPTION_MAX_LENGTH
    )
    keywords: list[Keyword] | None = Field(default=None, max_length=30)
    tags: list[Tag] | None = Field(default=None, max_length=30)
    canonical_url: str | None = Field(default=None, max_length=CANONICAL_URL_MAX_LENGTH)
    cover_image_url: str | None = Field(default=None, max_length=700)
    focus_keyword: str | None = Field(default=None, max_length=100)
    content_type: ContentType | None = None
    status: ContentStatus | None = None
    scheduled_for: datetime | None = None

    _check_cover = field_validator("cover_image_url")(_absolute_image_url)
    _check_canonical = field_validator("canonical_url")(_absolute_canonical_url)

    @field_validator("status")
    @classmethod
    def _settable(cls, value: ContentStatus | None) -> ContentStatus | None:
        if value is not None and value not in _SETTABLE_STATUSES:
            allowed = ", ".join(sorted(s.value for s in _SETTABLE_STATUSES))
            raise ValueError(
                f"'{value.value}' follows from this piece's publications and "
                f"cannot be set directly. Settable statuses: {allowed}."
            )
        return value


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


class LinkStatusOut(BaseModel):
    """One URL from the body, and whether it goes anywhere."""

    url: str
    #: "ok", "broken" or "unknown" — see app.services.link_check for why the
    #: third exists and why only "broken" blocks a publish.
    status: str
    http_status: int | None = None
    detail: str = ""


class LinkCheckOut(BaseModel):
    links: list[LinkStatusOut] = []
    #: Denormalized so the UI does not have to re-derive the headline number.
    broken_count: int = 0
    checked: int = 0


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
    cover_image_url: str | None = None
    focus_keyword: str = ""
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
    #: The shape this piece takes — article, thread or changelog. Derived from
    #: ``content_type`` rather than stored, so it can never disagree with it.
    content_format: str = "article"
    publications: list[PublicationOut] = []

    model_config = {"from_attributes": True}


class ContentDetail(ContentOut):
    """The list shape plus the body — the editor's payload.

    Kept separate so a list of 200 pieces doesn't ship 200 full post bodies.
    """

    body_markdown: str = ""
    seo_issues: list[SeoIssueOut] = []
    #: Everything wrong with this body *for its shape*: a post over the
    #: character limit, a changelog section that is not one of the six. Empty
    #: for an article, which has only SEO issues.
    format_issues: list[str] = []


class PublishRequestIn(BaseModel):
    """Queue a piece for one or more platforms."""

    platforms: list[Platform] = Field(min_length=1)
    #: ``None`` publishes as soon as a worker picks it up.
    scheduled_for: datetime | None = None
    #: Create it as a draft on the platform rather than going live.
    as_draft: bool = False
    #: Publish even though a link in the body is definitively dead. The gate
    #: exists because a 404 in a published post is embarrassing, not because it
    #: is unthinkable — sometimes the page is about to exist.
    allow_broken_links: bool = False

    _dedupe_platforms = field_validator("platforms")(unique_platforms)


class InternalLinkSuggestionOut(BaseModel):
    """Another post in the project worth linking to, by keyword overlap."""

    content_id: int
    title: str
    slug: str
    url: str | None = None
    matched_keywords: list[str] = []
    score: int


class SocialPreviewOut(BaseModel):
    """One network's rendering of the link card, as far as it is predictable."""

    network: str
    label: str
    #: Already clipped to this network's limit — see app.services.social_cards.
    title: str
    description: str
    title_clipped: bool = False
    description_clipped: bool = False
    domain: str = ""
    image_url: str | None = None
    #: "summary_large_image" or "summary". Decided entirely by whether there is
    #: a usable cover image, and it changes the whole shape of the card.
    card_type: str


class MetaTagOut(BaseModel):
    """One ``<meta>`` tag. A list, not a dict — ``article:tag`` repeats."""

    key: str
    value: str


class SocialCardsOut(BaseModel):
    """Everything the editor's social panel shows for one piece."""

    previews: list[SocialPreviewOut] = []
    #: Same shape and vocabulary as SeoIssueOut so the UI styles both alike.
    issues: list[SeoIssueOut] = []
    meta_tags: list[MetaTagOut] = []
    #: The tags as a pasteable ``<head>`` block, fully escaped.
    meta_html: str = ""
    recommended_image: dict[str, int] = {}


class RepurposeOut(BaseModel):
    """Social snippets derived from a long-form piece — nothing persisted."""

    twitter_thread: list[str] = []
    linkedin_post: str = ""
    provider: str | None = None
    model: str | None = None
    #: True when no AI provider was usable and this is the mechanical fallback
    #: (paragraph-split thread, excerpt-based LinkedIn post).
    is_fallback: bool = False


class InlineEditIn(BaseModel):
    """One passage of a draft, and what to do to it.

    ``selection`` is the passage itself rather than a pair of offsets. Offsets
    would be smaller to send and impossible to validate: the editor's copy of
    the body drifts from the stored one the moment anything is typed, and a
    stale offset pair silently edits the wrong paragraph. The text is checked
    against the stored body before any model call, so the failure is a 422 the
    author can act on rather than a replacement for something else.
    """

    selection: str = Field(
        min_length=inline_edit.MIN_SELECTION_CHARS,
        max_length=inline_edit.MAX_SELECTION_CHARS,
    )
    operation: inline_edit.EditOperation
    #: Only read by the ``retone`` operation. Left unset, that operation uses
    #: the project's own tone — "make this sound like the rest of my writing".
    tone: Tone | None = None

    @field_validator("selection")
    @classmethod
    def _not_only_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("select some text to edit")
        return v


class InlineEditOut(BaseModel):
    """The replacement for the selected passage — nothing persisted.

    The editor splices this into the textarea itself, which is what keeps the
    browser's own undo working and leaves the author holding the decision.
    """

    replacement: str
    operation: inline_edit.EditOperation
    provider: str | None = None
    model: str | None = None


class HeadlineVariantsOut(BaseModel):
    """Alternative headlines suggested for a piece — nothing applied yet."""

    variants: list[str] = []
    provider: str | None = None
    model: str | None = None
    is_fallback: bool = False


class HeadlineApplyIn(BaseModel):
    """Swap the live headline. Allowed even on published content."""

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)


class HeadlineWindowOut(BaseModel):
    """One headline and what it earned while it was live."""

    title: str
    started_at: datetime
    ended_at: datetime | None = None
    #: True for the headline live right now — its window has no end yet.
    current: bool = False
    #: Gained during the window, not the running total the snapshots carry —
    #: platform counters are cumulative. See app.services.headlines.
    views: int = 0
    engagement: int = 0
    #: How many metric snapshots landed in this window, so a caller can tell
    #: "no engagement" from "no data yet".
    snapshots: int = 0
    hours_live: float = 0.0
    #: The comparable number: views gained per day live. ``None`` until there
    #: is something to divide.
    views_per_day: float | None = None


class HeadlineWinnerOut(BaseModel):
    """Which headline is winning, and whether that is worth acting on."""

    title: str
    #: True only when the leader is not what is live *and* the evidence clears
    #: the bar. The only field an automated swap should read.
    confident: bool = False
    reason: str = ""
    score: float | None = None
    current_score: float | None = None
    ranked: list[HeadlineWindowOut] = []
    #: Set by the auto-select endpoint: whether the title actually changed.
    applied: bool = False


class BulkContentIn(BaseModel):
    """A batch of pieces to act on from the review queue."""

    content_ids: list[int] = Field(min_length=1, max_length=100)


class BulkPublishIn(BulkContentIn):
    platforms: list[Platform] = Field(min_length=1)
    scheduled_for: datetime | None = None
    as_draft: bool = False
    allow_broken_links: bool = False

    _dedupe_platforms = field_validator("platforms")(unique_platforms)


class BulkFailureOut(BaseModel):
    content_id: int
    reason: str


class BulkResultOut(BaseModel):
    """Per-item outcome — one bad piece in a batch must not sink the rest."""

    succeeded: list[int] = []
    failed: list[BulkFailureOut] = []


class ScheduleContentIn(BaseModel):
    """Put a piece on the calendar, or move the one that is already there."""

    #: Where it should go. Omitted means "the platforms it is already queued
    #: for", which is what a plain reschedule wants.
    platforms: list[Platform] | None = Field(default=None, min_length=1)
    #: When. Required unless ``optimize`` is set, and refused if both are.
    scheduled_for: datetime | None = None
    #: Let Herald pick the time per platform from the cadence table instead.
    #: Each platform gets its own slot, so a cross-post staggers rather than
    #: firing five copies into five feeds in the same second.
    optimize: bool = False
    as_draft: bool = False

    _dedupe_platforms = field_validator("platforms")(unique_platforms)


class SlotOut(BaseModel):
    """A proposed publish time for one platform, and why."""

    platform: Platform
    when: datetime
    rationale: str = ""


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


class PreviewLinkCreate(BaseModel):
    """How long the link should live. Omitted means the configured default."""

    ttl_hours: int | None = Field(default=None, gt=0)


class PreviewLinkOut(BaseModel):
    id: int
    url: str | None = None
    expires_at: datetime
    revoked_at: datetime | None = None
    view_count: int = 0
    last_viewed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PublicPreviewOut(BaseModel):
    """What an unauthenticated reviewer sees — the piece, nothing about who
    wrote it or where else it might go out."""

    title: str
    body_markdown: str
    excerpt: str
    cover_image_url: str | None = None
    word_count: int = 0
    read_minutes: int = 1
    project_name: str | None = None

    model_config = {"from_attributes": True}
