"""Content, publication and calendar schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
from app.schemas.limits import Keyword, Tag, Timezone
from app.services import inline_edit

#: The description every ``timezone`` field on a scheduling request carries.
#:
#: One string rather than four copies because the rule is subtle enough that a
#: client will read it, and four copies of a subtle rule become four rules.
TIMEZONE_HELP = (
    "IANA name — 'Europe/Berlin', 'America/New_York' — for reading a "
    "'scheduled_for' that carries no offset. Ignored when it does: a timestamp "
    "with an offset already names an instant. This is the durable way to say "
    "'09:00 local' for a date past the next daylight-saving change, which a "
    "client-side offset would get wrong by an hour."
)


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
    at all. The URL Pulse adopts on its own already has to pass this check (see
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
#: :func:`app.services.publishing_service.sync_content_status`, and setting
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


def _settable_status(value: ContentStatus | None) -> ContentStatus | None:
    """Refuse a status that is derived rather than chosen.

    Shared by ``ContentUpdate`` and ``ContentStatusIn`` — the PATCH and the
    dedicated transition endpoint are two doors to the same column, and a
    status settable through one but not the other is a way to write
    ``published`` onto a piece with nothing behind it.
    """
    if value is not None and value not in _SETTABLE_STATUSES:
        allowed = ", ".join(sorted(s.value for s in _SETTABLE_STATUSES))
        raise ValueError(
            f"'{value.value}' follows from this piece's publications and "
            f"cannot be set directly. Settable statuses: {allowed}."
        )
    return value


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int
    content_type: ContentType = ContentType.FEATURE_SPOTLIGHT
    #: Free text steering the piece — "focus on the Celery retry logic".
    instructions: str = Field(default="", max_length=2000)
    #: Pull the latest commits/releases from GitHub first. Off by default
    #: because it costs a round trip and only helps for release-driven pieces.
    include_repo_activity: bool = False


class ContentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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

    _settable = field_validator("status")(_settable_status)


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
    duration_ms: int | None = Field(
        default=None,
        description=(
            "How long the last attempt's call to this platform took, in "
            "milliseconds. Null until an attempt has actually reached the "
            "platform — a pending or scheduled row has one, and so does a row "
            "the circuit breaker parked before anything was sent — which is "
            "not the same as a call that took no time."
        ),
    )

    model_config = {"from_attributes": True}


class SeoIssueOut(BaseModel):
    level: str
    field: str
    message: str


class QualityOut(BaseModel):
    """What :mod:`app.services.quality` measured, and what it added up to.

    Every component is reported beside the total on purpose. ``score`` is the
    only number the DRAFT→REVIEW gate reads, and a caller told nothing but "48,
    too low" has been given a verdict with no way to act on it — whereas
    ``code_ratio: 0.93`` says which paragraph to write.
    """

    score: int = Field(
        description=(
            "0–100, combining the SEO score with readability and code density. "
            "The floor the review gate applies is "
            "`CONTENT_QUALITY_MIN_SCORE`."
        )
    )
    seo_score: int
    reading_ease: float | None = Field(
        default=None,
        description=(
            "Flesch Reading Ease over the prose, clamped to 0–100. Null for a "
            "body too short to measure — a social post, typically — where the "
            "formula's answer would be noise rather than a low score."
        ),
    )
    grade_level: float | None = Field(
        default=None, description="Flesch-Kincaid Grade Level. Null with reading_ease."
    )
    readability_points: int | None = Field(
        default=None,
        description=(
            "The reading ease as a 0–100 component of `score`. Null when there "
            "is no reading ease, in which case the component is dropped from "
            "the total rather than scored zero."
        ),
    )
    code_ratio: float = Field(
        description=(
            "Share of the body, by non-whitespace characters, inside fenced or "
            "inline code. 0–1."
        )
    )
    code_points: int
    words: int
    sentences: int


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
    #: How many times this row has been written, starting at 1. Send it back as
    #: ``If-Match`` on ``PATCH /content/{id}`` and the edit is refused with a 412
    #: if anyone else wrote the piece in between. See
    #: :attr:`app.models.content.Content.version`.
    version: int = 1
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
    #: Readability, code density, and the combined score the review gate reads.
    #: Computed from the same fields the SEO audit above is, so what the editor
    #: shows is what the gate will apply.
    quality: QualityOut | None = None


class PublishRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[Platform] = Field(min_length=1)
    #: ``None`` publishes as soon as a worker picks it up.
    scheduled_for: datetime | None = None
    timezone: Timezone | None = Field(default=None, description=TIMEZONE_HELP)
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


class PlatformEngagementOut(BaseModel):
    """One platform's newest reading for a piece.

    Every counter is optional and ``None`` means "this platform does not report
    it", which must stay distinct from zero — see
    ``app.services.content_engagement``.
    """

    publication_id: int
    platform: Platform
    external_url: str | None = None
    published_at: datetime | None = None
    #: ``None`` when the platform has never been polled, which is different from
    #: polled and reporting nothing.
    captured_at: datetime | None = None
    snapshots: int = 0
    engagement: int = 0
    views: int | None = None
    reads: int | None = None
    clicks: int | None = None
    reactions: int | None = None
    comments: int | None = None
    shares: int | None = None


class EngagementTotalsOut(BaseModel):
    """The piece's numbers added up, with the provenance of each one."""

    engagement: int = 0
    views: int | None = None
    reads: int | None = None
    clicks: int | None = None
    reactions: int | None = None
    comments: int | None = None
    shares: int | None = None
    #: Which platforms contributed to each field. A total without this reads as
    #: though the platforms that report nothing reported zero.
    reported_by: dict[str, list[str]] = {}


class EngagementPointOut(BaseModel):
    """One reading on the piece's trend line."""

    #: Hours since publication, so a syndicated copy lines up with the original
    #: rather than sitting to the right of it.
    hours: float
    views: int = 0
    engagement: int = 0


class ContentEngagementOut(BaseModel):
    """How one piece performed, everywhere it went."""

    content_id: int
    title: str
    platforms: list[PlatformEngagementOut] = []
    totals: EngagementTotalsOut = EngagementTotalsOut()
    trend: list[EngagementPointOut] = []


class PreflightFindingOut(BaseModel):
    """One thing wrong — or worth knowing — about a piece on one platform."""

    #: ``error`` means the attempt is wasted; ``warning`` means it publishes but
    #: not intact. See ``app.services.publishers.base.PreflightFinding``.
    level: str
    message: str
    #: Present when the finding is about a limit, so the UI can render "412/300"
    #: rather than only the sentence.
    limit: int | None = None
    actual: int | None = None


class PlatformCheckOut(BaseModel):
    """One platform's verdict on one piece."""

    platform: Platform
    #: False only when something *stops* the publish. A piece that will be
    #: shortened is publishable — that is what the short-form destinations are.
    publishable: bool
    findings: list[PreflightFindingOut] = []


class PlatformChecksOut(BaseModel):
    """Every destination this piece is bound for, checked."""

    content_id: int
    #: True when nothing anywhere is an error. The publish button reads this;
    #: it does not *have* to obey it — see ``app.services.platform_check``.
    publishable: bool
    platforms: list[PlatformCheckOut] = []


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
    model_config = ConfigDict(extra="forbid")

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
            raise ValueError(
                "selection is blank — highlight the passage you want to edit"
            )
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
    model_config = ConfigDict(extra="forbid")

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


class HeadlineUnreachableOut(BaseModel):
    """One destination not showing the piece's current headline."""

    publication_id: int
    platform: str
    #: The headline this destination is actually showing. ``None`` for a row
    #: published before Pulse recorded it.
    live_title: str | None = None
    reason: str


class HeadlineReachOut(BaseModel):
    """Which live destinations are showing the piece's current headline.

    The companion to ``/headlines/performance``: that endpoint reports what
    each headline earned, and this one reports how much of the audience was
    ever shown it. A piece published to Bluesky, LinkedIn and Buttondown has a
    reach of nothing — none of the three can retitle a live post — and its
    headline contest will never reach a verdict however much engagement it
    collects. Saying so is the difference between an honest answer and a
    broken-looking one.
    """

    #: Publications showing the current title, whose engagement counts as
    #: evidence in ``/headlines/performance``.
    tracking: int = 0
    #: Live publications in total.
    live: int = 0
    #: One entry per destination that is *not* showing it. ``reason`` is
    #: ``"unsupported"`` (the platform's API cannot retitle a live post) or
    #: ``"stale"`` (it can, and the last attempt did not land).
    unreachable: list[HeadlineUnreachableOut] = []


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
    model_config = ConfigDict(extra="forbid")

    content_ids: list[int] = Field(min_length=1, max_length=100)
    dry_run: bool = Field(
        default=False,
        description=(
            "Answer with the outcome and change nothing. Every check the real "
            "call makes is made here, in the same order and by the same code, "
            "so a piece reported as succeeding is one the real call would act "
            "on. That includes the link check on a bulk publish, which costs a "
            "network round trip per piece: a preview that skipped it would "
            "promise success for the pieces most likely to be refused."
        ),
    )


class BulkPublishIn(BulkContentIn):
    platforms: list[Platform] = Field(min_length=1)
    scheduled_for: datetime | None = None
    timezone: Timezone | None = Field(default=None, description=TIMEZONE_HELP)
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
    dry_run: bool = Field(
        default=False,
        description=(
            "Echoed from the request. On the wire so that a caller reading a "
            "response out of a log can tell a batch that ran from one that was "
            "only costed — the two bodies are otherwise identical, which is the "
            "point of a dry run and also the way to misread one."
        ),
    )
    #: ``len(succeeded) + len(failed)``, which is also ``len(content_ids)``:
    #: every id named gets exactly one verdict.
    #:
    #: Sent rather than left to the client to add up because this is the number
    #: a progress bar is drawn against, and a client that computes it has to
    #: know that the two lists partition the input — which is true, and is
    #: precisely the kind of invariant that stops being true one refactor later.
    total: int = 0

    @classmethod
    def of(
        cls,
        succeeded: list[int],
        failed: list[BulkFailureOut],
        *,
        dry_run: bool = False,
    ) -> BulkResultOut:
        """Build a result with ``total`` derived rather than passed.

        The one constructor the bulk endpoints use, so the count cannot be
        computed correctly in four places and wrongly in a fifth.
        """
        return cls(
            succeeded=succeeded,
            failed=failed,
            dry_run=dry_run,
            total=len(succeeded) + len(failed),
        )


class RetryResultOut(BaseModel):
    """What a piece-level retry re-armed.

    Separate from :class:`BulkResultOut` because the unit is different: that
    one reports per *piece*, this one reports how many *publications* on a
    single piece went back in the queue. A piece with three failed platforms
    is one success there and three here.
    """

    content_id: int
    retried: list[int] = Field(
        default=[], description="Publication ids put back in the queue."
    )
    skipped: list[BulkFailureOut] = Field(
        default=[],
        description=(
            "Publications left alone, with the reason — already published, or "
            "still in flight. Reported rather than silently ignored so a "
            "caller can tell 'nothing needed retrying' from 'nothing was "
            "retryable'."
        ),
    )


class ArchiveOldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    older_than_days: int = Field(
        ge=1,
        le=3650,
        description=(
            "Archive pieces created at least this many days ago. No default — "
            "an age-based bulk write should not have one, because the value "
            "that gets typed by accident is the one that was already there."
        ),
    )
    statuses: list[ContentStatus] | None = Field(
        default=None,
        description=(
            "Which statuses to sweep. Defaults to draft and review — the two "
            "that accumulate. Published is never swept whatever is asked for: "
            "archiving a live post hides the record of something that is still "
            "on the platforms."
        ),
    )
    project_id: int | None = Field(
        default=None, description="Limit the sweep to one project."
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Report what would be archived and change nothing. Worth doing "
            "first: this is the one endpoint here that writes to rows the "
            "caller has not named individually."
        ),
    )


class ArchiveOldOut(BaseModel):
    """What an age-based archive swept, or would have."""

    archived: list[int] = Field(
        default=[], description="The pieces archived, or on a dry run, the candidates."
    )
    count: int
    dry_run: bool


class ContentStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ContentStatus = Field(
        description=(
            "The status to move the piece to. The same rules the editor's own "
            "save applies: a published piece may only be archived, and moving "
            "a piece to approved releases it exactly as the Approve button "
            "does. `published` and `failed` are derived from the piece's "
            "publications and cannot be set here."
        )
    )

    _settable = field_validator("status")(_settable_status)


class ScheduleContentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[Platform] | None = Field(default=None, min_length=1)
    #: When. Required unless ``optimize`` is set, and refused if both are.
    scheduled_for: datetime | None = None
    #: Let Pulse pick the time per platform from the cadence table instead.
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
    model_config = ConfigDict(extra="forbid")

    scheduled_for: datetime | None = None
    timezone: Timezone | None = Field(default=None, description=TIMEZONE_HELP)
    #: Optional: move only this publication rather than the whole piece.
    publication_id: int | None = Field(default=None, le=2**31 - 1)


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
    model_config = ConfigDict(extra="forbid")

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
