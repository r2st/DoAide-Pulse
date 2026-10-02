"""Orchestration around the adapters: queueing, executing, recording.

The adapters know how to talk to a platform and nothing else. This module owns
everything stateful — which publications exist, what happened to them, when to
retry, and how a content row's status follows from its publications'.

The split matters for one reason above all: a piece of content going to three
platforms is three independent outcomes. Dev.to accepting and LinkedIn failing
is the ordinary case, and the piece is still "published". Only *every* platform
failing makes the content itself a failure.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.exc import StaleDataError

from app.config import settings
from app.models.content import CANONICAL_URL_MAX_LENGTH, Content, ContentStatus
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, elapsed_ms, utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import (
    EXTERNAL_ID_MAX_LENGTH,
    EXTERNAL_URL_MAX_LENGTH,
    Platform,
    Publication,
    PublicationStatus,
)
from app.models.translation import ContentTranslation
from app.models.webhook import WebhookEvent
from app.services import languages, publishers, translation, utm, webhook_payloads, webhooks
from app.services.crypto import CredentialEncryptionError, decrypt_credentials
from app.services.errors import clip_error, redact
from app.services.publishers import breaker, formatting
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
    PublishRequest,
    PublishResult,
    RateLimited,
    UnsupportedOption,
)

logger = logging.getLogger(__name__)


class NotConnected(PublishError):
    """The user has no live credentials for that platform."""


def not_connected_error(platform: Platform) -> str:
    """What a row says when the account has no live connection for *platform*.

    A function rather than an f-string at the raise site because two other
    places have to recognise the sentence again later:
    :func:`app.services.publish_recovery.recoverable` matches it to find the
    rows that a *new* connection un-blocks, and the tests pin it. A message
    only one caller knows the shape of is a message nothing can act on, and
    this failure is the one whose cure happens somewhere else entirely — in
    Settings, minutes or days after the row went terminal.
    """
    return f"No live {platform.value} connection — add one in Settings."


def queue(
    db: Session,
    content: Content,
    platforms: Sequence[Platform | str],
    *,
    scheduled_for: datetime | None = None,
    as_draft: bool = False,
) -> list[Publication]:
    """Create (or re-arm) a publication per platform. Does not publish.

    Re-queueing a platform that previously failed resets it rather than adding a
    second row — the unique constraint on (content, platform) makes that the
    only correct behaviour, and "retry this one" is the common case.

    When this batch contains both the piece's original and somewhere else to
    syndicate to, the copies are held back — see :func:`_syndication_schedule`.

    "Re-arm rather than add a second row" is decided from ``content.publications``,
    which is only as fresh as the moment the caller loaded the piece. Two requests
    that read it before either wrote — a double-clicked Publish button is the whole
    of what that takes — both saw no row for the platform and both inserted one,
    and the loser of that race hit the unique constraint. It surfaced as a 500 on a
    button whose second press should have been a no-op. So the insert goes in a
    savepoint: a conflict rolls back only this arming, the piece re-reads its
    publications, and the row the winner just wrote is re-armed exactly as a
    sequential re-queue would have. One retry, for the same reason
    ``routers.content`` retries a colliding slug once — a second conflict means a
    third writer, and the caller is better told than looped.
    """
    schedule = _syndication_schedule(content, platforms, base=scheduled_for)

    try:
        with db.begin_nested():
            out = _arm(db, content, platforms, schedule, scheduled_for, as_draft)
    except IntegrityError:
        # The savepoint has already taken the INSERTs back. What it does not undo
        # is the Python side: rows this attempt *modified* keep their new attribute
        # values on the instance, and one of those attributes — ``status`` — is
        # what the "already live" branch reads. Expiring the collection and then
        # the rows it comes back with puts both in step with the database, which
        # now includes the row the other writer committed.
        db.expire(content, ["publications"])
        for publication in content.publications:
            db.expire(publication)
        with db.begin_nested():
            out = _arm(db, content, platforms, schedule, scheduled_for, as_draft)

    # Canonical first, so a caller that dispatches in order gives the original a
    # head start even when the delay is switched off. Then the destinations that
    # host the article, ahead of the ones that only carry a link to it: when no
    # canonical platform is named, whichever publishes first becomes the piece's
    # canonical, and a plain alphabetical order handed that to Bluesky.
    canonical = _canonical_platform(content)
    out.sort(key=lambda p: (p.platform != canonical, *_original_rank(p.platform)))
    return out


def _arm(
    db: Session,
    content: Content,
    platforms: Sequence[Platform | str],
    schedule: dict[Platform, datetime | None],
    scheduled_for: datetime | None,
    as_draft: bool,
) -> list[Publication]:
    """One pass of :func:`queue`'s create-or-re-arm, flushed.

    Split out so it can be run twice: once optimistically, and once more against
    a re-read of ``content.publications`` when a concurrent writer got there
    first. It builds its own view of what already exists on every call, which is
    the whole point — the second call must not reuse the first call's.
    """
    existing = {p.platform: p for p in content.publications}
    out: list[Publication] = []

    for raw in platforms:
        platform = raw if isinstance(raw, Platform) else Platform(raw)
        publication = existing.get(platform)

        if publication is None:
            publication = Publication(content_id=content.id, platform=platform)
            db.add(publication)
            content.publications.append(publication)
        elif publication.status in (
            PublicationStatus.PUBLISHED,
            PublicationStatus.PUBLISHING,
        ):
            # Already live, or a worker has claimed it and is mid-flight.
            # Re-queueing a live row would double-post; re-arming one a
            # worker holds would overwrite the claim and leave the worker's
            # outcome — success or failure — writing back over whatever this
            # call set. The calendar's reschedule guards against PUBLISHING
            # for the same reason; this is the path it missed.
            out.append(publication)
            continue

        when = schedule.get(platform, scheduled_for)
        publication.status = (
            PublicationStatus.SCHEDULED if when else PublicationStatus.PENDING
        )
        publication.scheduled_for = when
        publication.as_draft = as_draft
        publication.error = None
        publication.attempts = 0
        out.append(publication)

    db.flush()
    return out


def _original_rank(platform: Platform) -> tuple[bool, bool, str]:
    """Sort key deciding which destination is a piece's original.

    Used both to order a batch and to pick the implicit original in
    :func:`_original_platform`, so the two cannot disagree about which URL a
    copy will end up pointing at.

    The order is: somewhere that hosts the article at all, then the user's own
    domain ahead of somebody else's platform, then alphabetically so the answer
    does not depend on the order the caller listed them in. Plain alphabetical
    was the whole of it before, which handed the canonical to Bluesky.
    """
    adapter = publishers.get_adapter(platform)
    return (not adapter.hosts_canonical, not adapter.owns_domain, platform.value)


def _canonical_platform(content: Content) -> Platform | None:
    """The platform whose URL this project treats as the original, if any."""
    project = content.project
    if project is None or not project.auto_canonical:
        return None
    return project.canonical_platform


def _original_platform(content: Content, wanted: set[Platform]) -> Platform | None:
    """The destination in *wanted* whose URL this piece will call its original.

    The project's named canonical platform when it has one and this batch
    contains it. Otherwise the *implicit* original: with no platform named,
    ``_adopt_canonical`` gives the title to whichever publishes first that hosts
    articles, so the first such platform by :func:`_original_rank` — the same
    order ``queue`` dispatches in — is the one everything else is a copy of.

    ``None`` when nothing here can hold a canonical URL — a batch of social
    posts has no original among it, and staggering them would delay posts that
    have nothing to wait for.
    """
    named = _canonical_platform(content)
    if named is not None:
        return named if named in wanted else None
    if content.project is None or not content.project.auto_canonical:
        return None
    hosts = sorted(
        (p for p in wanted if publishers.get_adapter(p).hosts_canonical),
        key=_original_rank,
    )
    return hosts[0] if hosts else None


def _syndication_schedule(
    content: Content,
    platforms: Sequence[Platform | str],
    *,
    base: datetime | None,
) -> dict[Platform, datetime | None]:
    """When each platform in this batch should go out.

    Publishing the original and its copies in the same instant loses the point
    of a canonical URL twice over: the copies are dispatched before the original
    has an ``external_url`` to be canonical *to*, and a crawler has no reason to
    believe the original came first. So when this batch contains both the
    original and somewhere else, the rest wait ``syndication_delay_seconds``
    behind it.

    "The original" is the project's named canonical platform when it has one,
    and the article-hosting destination that will win the race when it does not
    — see :func:`_original_platform`. Only honouring the named case left the
    default project (auto-canonical on, no platform named) publishing everywhere
    at once, which is the configuration every autopilot project on the box
    actually has.

    Returns a mapping for the platforms whose time differs from *base*; anything
    absent keeps the caller's own schedule. An empty dict means "nothing to
    stagger", which covers the ordinary single-platform publish.
    """
    delay = settings.syndication_delay_seconds
    if delay <= 0:
        return {}

    wanted = {p if isinstance(p, Platform) else Platform(p) for p in platforms}
    # Nothing to order: the original is not in this batch (so its URL either
    # already exists or is not coming), or there is nothing to syndicate.
    if len(wanted) < 2:
        return {}
    canonical = _original_platform(content, wanted)
    if canonical is None:
        return {}
    # The original already has its URL — the copies can go out immediately.
    if content.canonical_url:
        return {}

    later = (base or utcnow()) + timedelta(seconds=delay)
    return {platform: later for platform in wanted if platform != canonical}


#: What the row says once archiving takes it off the queue.
ARCHIVED_ERROR = "This piece was archived before it went out."

#: What the row says when the account that armed it has since been switched off.
#: Distinct from :data:`ARCHIVED_ERROR` because it is a different fact about a
#: different thing — the piece is fine, the account is not — and because it is
#: what the owner reads if they are ever switched back on.
DEACTIVATED_ERROR = "The account was deactivated before this went out."


def cancel_armed(db: Session, content: Content) -> list[Publication]:
    """Take *content* off the queue: cancel what has not run, drop its date.

    Archiving a piece is the user saying it is not going out, and it was the one
    way of saying that which nothing acted on. ``DELETE /content/{id}/schedule``
    cancels the rows; archiving set a column, left the schedule armed, and the
    beat sweep published the piece days later — so the review queue's Reject
    button posted the thing that had just been rejected.

    Terminal rows are left alone. Anything already live stays live: Pulse
    cannot unpublish, and rewriting a ``published`` row to ``cancelled`` would
    only lose the record of where the post is. Archiving a piece that is
    partly out cancels the copies that have not gone yet and nothing else.

    ``content.scheduled_for`` goes with them. It is the piece's own date rather
    than any platform's, and it is a queue entry in its own right: the calendar
    draws a piece that has no publications from that column alone, and the slot
    it sits in is one the cadence suggester then refuses to suggest. Clearing it
    here rather than at each call site is why ``POST /content/bulk/reject`` no
    longer leaves a rejected piece sitting on next Tuesday, which is the shape
    the single-piece path had already been fixed into by hand.

    Does not commit — every caller is inside a request that has more to write.
    """
    cancelled = [p for p in content.publications if not p.is_terminal]
    for publication in cancelled:
        publication.status = PublicationStatus.CANCELLED
        publication.scheduled_for = None
        publication.error = ARCHIVED_ERROR
    content.scheduled_for = None
    db.flush()
    return cancelled


def arming_approves(content: Content) -> None:
    """Arming a row on a draft is approving the draft. Make the column say so.

    ``draft`` and ``review`` are the statuses nothing is queued from — a piece in
    either has no armed publication, and ``routers.content._settle_status``
    cancels the queue when a piece is moved back to one. So any path that arms a
    row has to move the piece *out* of them, or it leaves a draft with a
    scheduled publication behind it: a contradiction the sweep resolves by
    publishing the draft. ``_queue_publish`` always did this; the retry
    endpoints and the calendar's reschedule, which re-arm a cancelled or failed
    row, did not.

    Only those two. ``failed`` is taken back by :func:`sync_content_status`
    once a row is armed, ``published`` stays published, and an archived piece
    is refused by every arming path before this is reached.
    """
    if content.status in (ContentStatus.DRAFT, ContentStatus.REVIEW):
        content.status = ContentStatus.APPROVED


def retry_hold(content: Content, publication: Publication) -> datetime | None:
    """When a hand-retried publication may go out, or ``None`` for right now.

    :func:`queue` parks the syndicated copies of a cross-post behind the
    original, and ``publish_tasks.publish_one`` refuses to claim a row before
    its time — between them the stagger survives whoever dispatches the batch.
    Retrying one publication went round both: ``routers.content.retry_publication``
    clears ``scheduled_for`` and dispatches the row on the spot, which is right
    for the case it was written for (a human clicking retry knows things the
    backoff does not) and wrong for a copy whose original has not published yet.
    The copy then goes out with no ``canonical_url`` on it — the one outcome the
    stagger exists to prevent — and unlike the queue-time race this one is
    permanent: the copy is live and there is nothing left to re-point at the
    original.

    So a copy is held the same distance behind the original that :func:`queue`
    would have held it, measured from when the original is actually expected
    rather than from now. Everything else retries immediately, because there is
    nothing to wait for:

    * the stagger is switched off, or this piece already has its canonical URL;
    * this *is* the original, whose whole job is to go first;
    * no destination here can hold a canonical URL, or the project opted out of
      canonical handling — :func:`_original_platform` answers ``None`` to both;
    * the original has no publication row, or its row is terminal. A failed,
      cancelled or already-published original is not going to produce a URL that
      does not exist yet, and waiting on it would delay the copy for nothing.

    The wait is bounded by the same reasoning as the queue-time one: the hold is
    a fixed delay, not a dependency, so a copy whose original never publishes
    goes out on its own once the delay elapses rather than waiting forever.
    """
    delay = settings.syndication_delay_seconds
    if delay <= 0 or content.canonical_url:
        return None

    canonical = _original_platform(content, {p.platform for p in content.publications})
    if canonical is None or canonical == publication.platform:
        return None

    original = next(
        (p for p in content.publications if p.platform == canonical), None
    )
    if original is None or original.is_terminal:
        return None

    now = utcnow()
    # The original's own schedule when it has one, so a copy retried while the
    # original is still parked waits for the original rather than for a delay
    # counted from the click. An overdue original is due now, not in the past.
    due = as_aware(original.scheduled_for) if original.scheduled_for else now
    return max(due, now) + timedelta(seconds=delay)


def _credentials_for(db: Session, user_id: int, platform: Platform) -> dict:
    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
        )
    )
    if connection is None or connection.status != ConnectionStatus.CONNECTED:
        raise NotConnected(not_connected_error(platform))
    try:
        return decrypt_credentials(connection.encrypted_credentials)
    except CredentialEncryptionError as exc:
        # A key change is a credential problem, not a transient one.
        raise CredentialError(str(exc)) from exc


def _redact_credentials(
    exc: PublishError, adapter: publishers.Adapter, credentials: dict[str, Any]
) -> None:
    """Strip this connection's secrets out of *exc*, in place.

    Mutating ``args`` rather than raising a fresh exception because the type and
    the attributes on it are load-bearing: ``RateLimited.retry_after`` decides
    how long the row is parked, ``PublishError.status_code`` decides whether a
    Git lookup reads as "new post", and the caller re-raises to arms that match
    on type. Rebuilding the exception would have to know all of that.

    Only the fields the adapter declared ``secret`` are removed. A WordPress
    site URL and a Bluesky handle are in the message on purpose — they say
    *which* connection failed — and a repo name is half the useful part of a
    GitHub error.
    """
    secrets = publishers.secret_values(adapter, credentials)
    if not secrets:
        return
    exc.args = tuple(
        redact(arg, secrets) if isinstance(arg, str) else arg for arg in exc.args
    )


def _translation_for(
    db: Session, user_id: int, platform: Platform, content: Content
) -> translation.PublishChoice:
    """Which text this destination should receive, and why.

    Reads the language off the connection, which is the only place that fact
    lives. A destination with no connection row cannot be published to at all —
    ``_credentials_for`` has already raised by the time this runs in ``execute``
    — so the ``None`` branch here is for the callers that build a request
    without one, and it answers with the original, which is what every caller
    got before languages existed.

    The reason is logged rather than only returned. A fallback to English is the
    outcome a user is least likely to notice and most likely to care about, and
    the place they will look when they do notice is the worker log for the
    publication that surprised them.
    """
    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
        )
    )
    wanted = connection.language if connection is not None else None
    choice = translation.for_publishing(db, content, wanted)
    if wanted and languages.normalize(wanted) not in (None, languages.SOURCE_LANGUAGE):
        logger.info(
            "publishing content %s to %s in %s: %s",
            content.id,
            platform.value,
            choice.language,
            choice.reason,
        )
    return choice


def _mark_connection_invalid(
    db: Session, user_id: int, platform: Platform, error: str
) -> None:
    connection = db.scalar(
        select(PlatformConnection).where(
            PlatformConnection.user_id == user_id,
            PlatformConnection.platform == platform,
        )
    )
    if connection is not None:
        connection.status = ConnectionStatus.INVALID
        connection.last_error = clip_error(error)


def build_request(
    content: Content,
    *,
    platform: Platform | None = None,
    as_draft: bool = False,
    translation: ContentTranslation | None = None,
) -> PublishRequest:
    """The flat value object adapters take, built from a content row.

    Called while the session is open so the ``project`` relationship is loaded
    before the worker touches it.

    *platform* is what makes the request platform-specific without making the
    adapters stateful: it supplies the campaign source for the outbound links
    and the idempotency key. It is optional so that callers who only want to
    preview the shared parts — the SEO panel, tests — need not name one.

    *translation* swaps the four fields that have words in them for their
    other-language versions, and nothing else. Not the slug, which is the
    filename a Git destination writes and the ``utm_content`` on the share link
    — changing it per language would publish the same piece to two paths and
    split its analytics in half. Not the tags or keywords, which are a taxonomy
    the destination indexes on rather than prose. Not the canonical, which must
    keep pointing at the one original. The rule is the one
    :mod:`app.models.translation` opens with: there is one piece, and a
    language is an adaptation of it, exactly like a platform.

    Resolving *which* translation is not this function's job — see
    :func:`app.services.translation.for_publishing`, which is the only caller
    allowed to decide, because deciding wrongly means publishing fluent prose
    that describes a version of the piece that no longer exists.
    """
    project = content.project
    tagged = _Campaign(content, platform)
    text = translation if translation is not None else content

    return PublishRequest(
        title=text.title,
        body_markdown=tagged.markdown(text.body_markdown),
        # Flattened, because for four of the ten destinations this *is* the post.
        #
        # Mastodon, Bluesky, Twitter and LinkedIn all compose from
        # ``excerpt or …``, so the excerpt is the first term in every one of
        # them and the fallbacks behind it rarely run. The body reaching those
        # composers goes through ``formatting.to_plain_text`` — that is what
        # stops a ``<script>`` in a body being posted as the literal text
        # ``alert('xss')`` under the author's name — and the excerpt beside it
        # went out exactly as written. Same provenance as the body (a model
        # writing from somebody else's README, an RSS trigger, a paste), same
        # treatment.
        #
        # Here rather than in the four composers because that is the shape of
        # bug this is: one term short in the component nobody demos. A
        # destination added next year gets a plain-text excerpt without having
        # to know why.
        excerpt=formatting.to_plain_text(text.excerpt or ""),
        meta_description=text.meta_description,
        tags=list(content.tags or []),
        keywords=list(content.keywords or []),
        focus_keyword=getattr(content, "focus_keyword", "") or "",
        slug=content.slug,
        canonical_url=content.canonical_url,
        share_url=tagged.url(
            content.canonical_url or (project.live_url if project else None)
        ),
        cover_image_url=content.cover_image_url,
        project_url=tagged.url(project.live_url if project else None),
        project_name=project.name if project else "",
        as_draft=as_draft,
        idempotency_key=(
            f"herald-{content.id}-{platform.value}" if platform and content.id else None
        ),
        language=translation.language if translation is not None else languages.SOURCE_LANGUAGE,
    )


#: Cap on ``utm_content``. The slug it is built from is derived from the title,
#: which the API allows up to 300 characters — and ``utm_content`` lands on the
#: share link *twice over* once the slug is in the path as well. On Bluesky,
#: which shortens nothing and allows 300 characters for the whole post, that is
#: the entire budget spent on the query string of the post's own link.
#:
#: Sixty is enough to tell two posts apart in an analytics report, which is all
#: this value is for. Two titles that differ only past character sixty collapse
#: to the same label; that is a worse report than the alternative would be, and
#: a far better one than a post that never went out.
_UTM_CONTENT_MAX = 60


def _utm_content(slug: str) -> str:
    """A slug trimmed to something that belongs in a query string."""
    return (slug or "")[:_UTM_CONTENT_MAX].rstrip("-")


class _Campaign:
    """Applies one project's UTM parameters, for one platform.

    Built once per request so the parameters cannot drift between the share
    link and the links inside the body — an article whose byline link is
    attributed to Dev.to and whose body links are attributed to nothing is
    worse than either alone.
    """

    def __init__(self, content: Content, platform: Platform | None) -> None:
        project = content.project
        # No platform means no honest ``utm_source``, and inventing one would be
        # worse than not tagging: it looks like data.
        self.active = bool(
            platform is not None and project is not None and project.utm_enabled
        )
        self.host = utm.host_of(project.live_url) if project else ""
        self.params = (
            {
                "source": platform.value,
                "medium": publishers.get_adapter(platform).utm_medium,
                "campaign": project.campaign,
                "content": _utm_content(content.slug),
            }
            if self.active and platform and project
            else {}
        )

    def url(self, url: str | None) -> str | None:
        """Tag one URL, or hand it back untouched when tagging is off.

        Passes ``None`` through, because the fields this tags — the canonical
        and the project link — are optional, and a caller should not have to
        guard every one of them.
        """
        return utm.tag(url, **self.params) if self.active else url

    def markdown(self, body: str) -> str:
        """Tag the body's links to the project's own site, and nothing else."""
        if not self.active:
            return body
        return utm.tag_markdown_links(body, host=self.host, **self.params)


def execute(db: Session, publication: Publication) -> Publication:
    """Attempt one publication, recording the outcome. Never raises.

    Retryable failures park the row until its backoff has elapsed and
    ``publish_max_retries`` is spent (see :func:`_fail`); credential failures
    and unfinished adapters go straight to ``failed``, because retrying either
    is guaranteed to fail the same way. A worker running out of time is
    retryable too — see the ``SoftTimeLimitExceeded`` branch, which has to come
    before the defensive catch-all rather than rely on the handler in
    ``publish_tasks.publish_one``.
    """
    content = publication.content

    # Last gate before a platform is contacted, and the only one that sees the
    # piece rather than the row. ``publish_one``'s claim is a conditional UPDATE
    # over ``publications`` alone, so it cannot know the piece was archived —
    # and the window is real: the beat sweep picks up rows armed days earlier,
    # and a worker holding a row for the length of an HTTP call is long enough
    # for someone to archive the piece in front of it. The archiving paths
    # cancel these rows themselves (:func:`cancel_armed`); this is what catches
    # the row that was already in flight, and any future path that forgets.
    #
    # Cancelled rather than failed: nothing failed, the piece was withdrawn.
    if content.status == ContentStatus.ARCHIVED:
        publication.status = PublicationStatus.CANCELLED
        publication.scheduled_for = None
        publication.error = ARCHIVED_ERROR
        db.commit()
        logger.info(
            "publication %s to %s dropped: content %s is archived",
            publication.id,
            publication.platform.value,
            content.id,
        )
        return publication

    # The same gate, for the account rather than the piece — and it belongs here
    # for the reason the comment above gives, because the window is the same one
    # and wider. ``release_approved_content`` will not *arm* anything for a
    # deactivated account, but a row armed while the account was live outlives
    # the switch-off: ``due_publications`` selects on status and time alone, so
    # a piece scheduled for next Tuesday went out on Tuesday, to a platform,
    # under credentials belonging to an account Pulse had been told to stop.
    #
    # Deactivation means the whole account everywhere else — tokens
    # (:func:`app.deps.get_current_user`), preview links
    # (:func:`app.services.preview_links.resolve`), inbound triggers
    # (:func:`app.services.triggers.fire`), and the sweeps that scan, write,
    # mail and poll. Publishing was the one path that could still act outward on
    # a switched-off account's behalf, which made it the one that mattered most.
    #
    # Cancelled rather than failed, as above: nothing failed, and the row is not
    # waiting for a retry that must never happen.
    owner = content.project.user
    if owner is None or not owner.is_active:
        publication.status = PublicationStatus.CANCELLED
        publication.scheduled_for = None
        publication.error = DEACTIVATED_ERROR
        db.commit()
        logger.info(
            "publication %s to %s dropped: account %s is deactivated",
            publication.id,
            publication.platform.value,
            content.project.user_id,
        )
        return publication

    user_id = content.project.user_id
    adapter = publishers.get_adapter(publication.platform)

    # Third gate, and the only one about the *route* rather than about the piece
    # or the account. A platform this account has just failed against four times
    # running is one every other row in the queue is about to fail against too,
    # and each of them would spend an attempt finding that out. Parking here
    # costs the row a wait; not parking costs it a retry it will want later.
    #
    # Before the attempt counter, deliberately. The budget is for attempts that
    # reached a platform — charging a row for a request that was never sent is
    # how a piece arrives at terminal ``failed`` during an outage it never
    # touched, which is the failure this whole layer exists to prevent.
    #
    # **Only a row that has not been tried yet.** A row already in the retry
    # cycle is let through however shut the route looks, and that asymmetry is
    # the whole design rather than an oversight. ``_fail`` and ``_defer``
    # guarantee that a platform failing every attempt ends up terminal, in front
    # of a human, after ``publish_max_retries`` — a guarantee a breaker that
    # could hold any row indefinitely would quietly repeal, leaving a queue that
    # waits forever on an outage nobody is told about. So the row that first met
    # the outage stays the canary and keeps its own escalation, and the ones
    # behind it are spared: one report per outage instead of thirty, which is
    # both the cheaper answer and the more readable one.
    if publication.attempts == 0 and breaker.is_open(publication.platform, user_id):
        _defer_for_breaker(db, publication, user_id)
        return publication

    publication.status = PublicationStatus.PUBLISHING
    publication.attempts += 1
    db.flush()

    try:
        credentials = _credentials_for(db, user_id, publication.platform)
        # Which language this destination wants, and whether there is a
        # translation fit to go out in it. Resolved here rather than when the
        # row was queued, because both halves of the answer move in between: a
        # translation queued as ready can be made stale by an edit, and one
        # queued as pending can have finished. The queue decides *that* a piece
        # publishes; this decides what text.
        choice = _translation_for(db, user_id, publication.platform, content)
        # Bound to a name rather than built inline because the success path
        # below records ``request.title`` as the headline this destination now
        # shows — see ``publication.live_title``.
        request = build_request(
            content,
            platform=publication.platform,
            as_draft=publication.as_draft,
            translation=choice.translation,
        )
        # The clock starts *here*, after the credentials are read and decrypted,
        # and stops in the ``finally`` below. What is being measured is the
        # platform's latency and nothing else: a row can sit scheduled for a
        # week, and this worker spends time either side of the request on
        # database reads, canonical URLs and a webhook. Folding any of that in
        # would make the number useless for the question it exists to answer.
        started = time.monotonic()
        try:
            result = adapter.publish(request, credentials)
        except PublishError as exc:
            # The last point the credential values are known, and the last point
            # before this message becomes a row. Everything below writes it to
            # ``publication.error`` or ``connection.last_error`` — plaintext
            # ``Text`` columns rendered in the UI — and adapter messages quote
            # the platform's response body on purpose. A platform that validates
            # by echoing ("invalid api_key: …") would put the token in the
            # database beside the encrypted copy of itself.
            _redact_credentials(exc, adapter, credentials)
            raise
        finally:
            # In a ``finally`` so it is recorded whichever way the call ended,
            # and a failed attempt is the half that matters most: a platform
            # taking forty seconds to refuse a post is what puts a worker on its
            # soft time limit, and timing only the successes would report that
            # platform's latency as its good days.
            #
            # Assigned before ``_fail`` and ``_defer`` run — both commit — so
            # the duration lands in the same transaction as the outcome rather
            # than needing a second write.
            publication.duration_ms = elapsed_ms(started)
    except (NotConnected, NotImplementedAdapter, UnsupportedOption) as exc:
        # None of these is transient: no amount of retrying connects an account,
        # finishes an adapter, or gives a platform a feature it does not have.
        #
        # And none of them is evidence about the route, so the breaker is not
        # told. Each is a fact about *this request* that a perfectly healthy
        # platform would state the same way every time, and a breaker that
        # counted them would open on a platform with nothing wrong with it and
        # then park the rows that would have gone out.
        _fail(db, publication, str(exc), terminal=True)
        return publication
    except CredentialError as exc:
        # Not counted either, for the same reason: a rejected token is this
        # connection's problem. The connection is marked invalid below, which is
        # the thing that actually stops the other rows trying.
        _mark_connection_invalid(db, user_id, publication.platform, str(exc))
        _fail(db, publication, str(exc), terminal=True)
        return publication
    except RateLimited as exc:
        # The platform said when to come back, and coming back sooner is how a
        # soft limit becomes a ban. Park the row until then rather than leaving
        # it `pending` for the next sweep, which is minutes away at most.
        #
        # The breaker is told, and told the window: a rate limit is charged
        # against this account's key, so every other row queued for this
        # platform on this account is about to be refused the same way. Parking
        # them behind one refusal is the difference between a queue that waits
        # out a limit and a queue that hardens it into a ban.
        breaker.record_failure(
            publication.platform, user_id, retry_after=exc.retry_after
        )
        _defer(db, publication, exc)
        return publication
    except PublishError as exc:
        # The one that is evidence about the route: "something went wrong while
        # publishing, and it might not recur". One of these is a blip; four in a
        # row is a platform.
        breaker.record_failure(publication.platform, user_id)
        _fail(
            db,
            publication,
            str(exc),
            terminal=publication.attempts >= settings.publish_max_retries,
        )
        return publication
    except SoftTimeLimitExceeded:
        # Celery delivers the worker's soft time limit by raising *inside*
        # whatever the task is doing, which is nearly always the HTTP call
        # above — that is where a publish spends its time. And it raises an
        # ordinary ``Exception``, not a ``BaseException``, so without this
        # branch the defensive handler below caught it, wrote "Unexpected
        # error" on the row and marked it terminal: the first time a platform
        # was slow, the post was burned. ``publish_tasks.publish_one`` has a
        # handler meant for this, but it cannot run — nothing propagates out of
        # a function that has already caught the exception and returned.
        #
        # A timeout says nothing about the post, only about how long the
        # platform took, so it goes on the same budget as any other retryable
        # failure rather than ending the row.
        # Counted: a platform too slow to answer inside the worker's soft limit
        # is a platform the next row will also be too slow for.
        breaker.record_failure(publication.platform, user_id)
        _fail(
            db,
            publication,
            TIMEOUT_ERROR,
            terminal=publication.attempts >= settings.publish_max_retries,
        )
        return publication
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected error publishing %s", publication.id)
        _fail(db, publication, f"Unexpected error: {exc}", terminal=True)
        return publication

    # The route works. Discard whatever run of failures preceded this rather
    # than decrementing it: a half-remembered outage from an hour ago would
    # otherwise trip the breaker early on the next unrelated blip.
    breaker.record_success(publication.platform, user_id)

    # Captured before the sync, because the interesting moment is the
    # *transition* — a piece already live on Dev.to going out on Mastodon is
    # not news, and firing "published" per platform would make the event mean
    # something different from its name.
    was_published = content.status == ContentStatus.PUBLISHED
    published_at = utcnow()
    # The headline the reader will actually see, which is the request's and not
    # necessarily the piece's: a translated publication carries the translated
    # title. Recorded so a later headline swap can tell which destinations it
    # has reached — see app.services.headline_sync.
    live_title = request.title[:300]

    def _record(publication: Publication) -> None:
        publication.status = PublicationStatus.PUBLISHED
        publication.published_at = published_at
        publication.external_id = _recorded(
            result.external_id,
            EXTERNAL_ID_MAX_LENGTH,
            publication=publication,
            field="external_id",
        )
        publication.external_url = _recorded(
            result.external_url,
            EXTERNAL_URL_MAX_LENGTH,
            publication=publication,
            field="external_url",
        )
        publication.live_title = live_title
        publication.error = None
        _adopt_canonical(publication.content, publication, result)
        sync_content_status(publication.content)

    _commit_outcome(db, publication, _record)
    content = publication.content
    if not was_published and content.status == ContentStatus.PUBLISHED:
        _notify_published(db, content, publication)
    logger.info(
        "published content %s to %s in %dms: %s",
        content.id,
        publication.platform.value,
        publication.duration_ms or 0,
        result.external_url,
    )
    return publication


def _recorded(
    value: str | None, limit: int, *, publication: Publication, field: str
) -> str | None:
    """A platform's answer, or ``None`` when the column cannot hold it.

    Both columns are written straight from a response body. Dev.to and Medium
    are fixed hosts answering with their own permalinks, but WordPress, Mastodon
    and Bluesky are servers the *user* named — the same untrusted-response
    problem :meth:`Adapter._require_public_url` guards the request side of — and
    a long enough ``link`` field is all it takes to hand this assignment a value
    the column will not take.

    The cost of that is out of all proportion to the field. On PostgreSQL the
    over-long value raises ``StringDataRightTruncation`` from the commit below,
    which is the commit recording the post as ``PUBLISHED``. It rolls back, the
    row stays ``publishing``, and the reclaim sweep re-arms it — so a post that
    is already live on the platform is published a second time. Dropping the
    field costs a link in the UI, or metrics for one post; keeping it costs a
    duplicate.

    Truncating instead is worse than dropping: a URL cut at 700 characters is a
    link that goes somewhere else, and a post id cut at 200 fetches somebody
    else's metrics. ``_adopt_canonical`` declines for the same reason.
    """
    if value is None or len(value) <= limit:
        return value
    logger.warning(
        "publication %s: %s from %s is %d characters, over the %d the column "
        "holds — recording the publication without it",
        publication.id,
        field,
        publication.platform.value,
        len(value),
        limit,
    )
    return None


def _notify_published(db: Session, content: Content, publication: Publication) -> None:
    """Tell this user's webhooks that a piece is public.

    Emitted after the commit that made it true, so an endpoint that turns round
    and reads the API back sees what the payload describes.
    """
    project = content.project
    webhooks.emit(
        db,
        user_id=project.user_id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        data={
            "content": webhook_payloads.content_payload(content),
            "publication": webhook_payloads.publication_payload(publication),
        },
    )


def _failure_notice(publication: Publication) -> tuple[int, dict] | None:
    """Who to tell that this publication is finished, and what to tell them.

    Split from :func:`_notify_failed` so a caller with a *batch* of them can
    build the bodies while its rows are still loaded and send them afterwards.
    Reading a payload out of an expired row costs the queries the row's own
    eager load was there to save — see :func:`reclaim_stuck`, the only caller
    that fails more than one publication at a time.

    ``None`` when there is no owner to tell.
    """
    content = publication.content
    project = content.project if content else None
    if project is None:  # pragma: no cover - a publication always has a project
        return None
    return project.user_id, {
        "content": webhook_payloads.content_payload(content),
        "publication": webhook_payloads.publication_payload(publication),
    }


def _notify_failed(db: Session, publication: Publication) -> None:
    """Tell this user's webhooks that a platform gave up on a piece.

    Only for terminal failures. A mid-retry blip is not something to page
    anyone about, and an endpoint told about all three attempts learns nothing
    it did not know after the third.
    """
    notice = _failure_notice(publication)
    if notice is None:  # pragma: no cover - a publication always has a project
        return
    user_id, data = notice
    webhooks.emit(
        db,
        user_id=user_id,
        event=WebhookEvent.PUBLICATION_FAILED,
        data=data,
    )


def _adopt_canonical(
    content: Content, publication: Publication, result: PublishResult
) -> None:
    """Adopt this publication's URL as the piece's canonical, when it should be.

    Pulse's syndication model has always depended on ``canonical_url`` — every
    adapter sends it — but nothing set it, so in practice each copy was published
    with no original and the search ranking was split between them. This closes
    that: publish somewhere, and everywhere afterwards is told where the real one
    lives.

    Five things stop it firing, and each is a case where guessing would be worse
    than leaving the field empty:

    * the project opted out, or a human already typed a canonical URL — an
      explicit answer beats an inferred one;
    * the project names a canonical platform and this is not it, so this URL is
      itself a copy;
    * the destination does not host articles — see
      :attr:`app.services.publishers.base.Adapter.hosts_canonical`. A project
      with no canonical platform named lets whichever platform publishes first
      win, and ``queue`` orders an untitled race alphabetically, so Bluesky beat
      Dev.to every time and the article's canonical became a 300-character post
      linking to it. A crawler reading that is told the microblog post is the
      original and the article is the copy;
    * the post was staged as a draft, whose URL is a private editor link that
      would 404 for a crawler;
    * the platform did not return an absolute ``http(s)`` URL to use;
    * the URL is longer than the column that would hold it — see below.
    """
    project = content.project
    if project is None or not project.auto_canonical or content.canonical_url:
        return
    if project.canonical_platform and publication.platform != project.canonical_platform:
        return
    if not publishers.get_adapter(publication.platform).hosts_canonical:
        return
    if publication.as_draft:
        return

    url = (result.external_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return
    # ``external_url`` is String(700) and ``canonical_url`` is String(500), so a
    # URL a platform happily returned can be one this column cannot hold. That
    # made this the worst place in the tree to overflow a column: the post is
    # already live, the request that published it is what raises, and on
    # PostgreSQL a StringDataRightTruncation here rolls back the same commit
    # that records the publication as PUBLISHED — so the piece is on the
    # platform and Pulse still believes it is not. Declining to adopt is the
    # sixth case where leaving the field empty beats guessing; every adapter
    # already handles an empty canonical, and the author can still type one.
    if len(url) > CANONICAL_URL_MAX_LENGTH:
        logger.info(
            "content %s did not adopt %s's URL as canonical: %d characters, "
            "over the %d the column holds",
            content.id,
            publication.platform.value,
            len(url),
            CANONICAL_URL_MAX_LENGTH,
        )
        return

    content.canonical_url = url
    logger.info(
        "content %s adopted %s as its canonical URL: %s",
        content.id,
        publication.platform.value,
        url,
    )


def _defer(db: Session, publication: Publication, exc: RateLimited) -> None:
    """Park a rate-limited publication until the platform is ready for it.

    Counts against the same retry budget as any other failure — a platform that
    rate-limits every attempt is a problem a human should see, not one to keep
    quietly re-queueing — but the row goes ``scheduled`` rather than ``pending``
    so the next sweep skips it until the wait is up.
    """
    if publication.attempts >= settings.publish_max_retries:
        _fail(db, publication, str(exc), terminal=True)
        return

    wait = exc.retry_after
    if wait is None:
        # The platform declined to say when. Fall back to the same escalating
        # window any other retryable failure gets rather than a flat sweep
        # interval: a platform limiting us on every attempt is one to back away
        # from, and coming back at a fixed cadence is how a soft limit hardens.
        wait = retry_defer_seconds(publication.attempts)
    wait = min(wait, float(settings.publish_rate_limit_max_defer_seconds))

    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = utcnow() + timedelta(seconds=wait)
    publication.error = clip_error(f"{exc} — retrying in {round(wait)}s")
    db.commit()
    logger.info(
        "publication %s to %s rate-limited (attempt %d); deferred %.0fs",
        publication.id,
        publication.platform.value,
        publication.attempts,
        wait,
    )


#: What a publication's ``error`` says while its route is being skipped. Not a
#: failure message — nothing failed and nothing was sent — but the ``error``
#: column is the one the publications list renders, and a row that has silently
#: moved twenty minutes into the future with nothing beside it is the state an
#: operator cannot tell from a bug.
BREAKER_OPEN_ERROR = (
    "Paused: recent publishes to this platform have been failing. "
    "Waiting for it to recover before trying again."
)


def _defer_for_breaker(db: Session, publication: Publication, user_id: int) -> None:
    """Park a publication for as long as its route stays shut. Commits.

    Held for exactly the breaker's own remaining window rather than for a
    backoff of this row's invention: the breaker has said when it will let the
    route be tried again, and a row that comes back before then only re-parks
    itself, one dispatch at a time, for the whole cooldown.

    ``scheduled`` and no attempt spent — see the gate in :func:`execute`. A
    floor of one sweep interval because a window that has almost elapsed would
    otherwise put the row back in front of the very next sweep, which is the
    busy-wait this is here to avoid.
    """
    wait = max(
        breaker.seconds_remaining(publication.platform, user_id),
        float(settings.publish_scan_interval_seconds),
    )
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = utcnow() + timedelta(seconds=wait)
    publication.error = clip_error(BREAKER_OPEN_ERROR)
    db.commit()
    logger.info(
        "publication %s to %s held %.0fs: the breaker for %s is open",
        publication.id,
        publication.platform.value,
        wait,
        breaker.key(publication.platform, user_id),
    )


#: What a publication's ``error`` says when the worker ran out of time.
#:
#: Named because two paths write it and they must not drift: :func:`execute`,
#: for the timeout that lands inside its own ``try`` — which is nearly all of
#: them, since a publish spends its time in the adapter's HTTP call — and
#: :func:`record_timeout`, for the one that lands anywhere else.
TIMEOUT_ERROR = "Publishing timed out — the platform took too long to answer"


def record_timeout(db: Session, publication: Publication) -> None:
    """Count a publish that ran out of time outside :func:`execute`. Commits.

    Celery delivers a soft time limit by raising inside whatever the task is
    doing, and *nearly* always that is the adapter's HTTP call — which sits
    inside :func:`execute`'s ``try``, where the timeout is caught and put on the
    retry budget like any other transient failure. This is for the rest: a
    timeout during the claim, or during the lazy loads in ``execute``'s prologue
    that walk from the publication to its content, project and owner.

    ``publish_tasks.publish_one`` used to answer that by setting the row back to
    ``pending`` and nothing else, which had two problems and they compound.

    The first is that the attempt was not counted. ``execute`` increments
    ``attempts`` and *flushes*; it does not commit, and nothing between that
    flush and the adapter call does either. A timeout in the prologue therefore
    rolls the increment back with the transaction, and one that fires before
    ``execute`` is even reached never made it. So the row came back at the count
    it went in with — a free retry, which is precisely the loop
    :func:`reclaim_stuck` documents at length and guards against for the worker
    that gets killed instead of timing out: re-arm, time out, roll back, re-arm
    at the same number, forever, and never an error anybody is shown. The caller
    counts the attempt here for the same reason ``reclaim_stuck`` counts it
    there — reaching this function is proof the attempt happened.

    The second is that ``pending`` is due immediately, so even a counted attempt
    would have been spent at the sweep cadence rather than backed off. Going
    through :func:`_fail` puts a timeout on exactly the terms every other
    retryable failure gets: parked ``scheduled`` behind an escalating window,
    and terminal — with the ``publication.failed`` webhook that goes with it —
    once the budget is gone.
    """
    publication.attempts += 1
    _fail(
        db,
        publication,
        TIMEOUT_ERROR,
        terminal=publication.attempts >= settings.publish_max_retries,
    )


def retry_defer_seconds(attempts: int) -> float:
    """How long to hold a publication before spending its next attempt.

    Exponential from ``publish_retry_defer_seconds``, capped at
    ``publish_retry_max_defer_seconds``. Unlike the in-adapter backoff in
    :func:`app.services.publishers.base._backoff_delay` this is not jittered:
    nothing sleeps on it — the row carries a ``scheduled_for`` and the sweep
    picks it up — so there is no thundering herd to spread out, and a
    predictable "back in five minutes" is what the publications list can show.

    ``attempts`` is the count *already spent*, so the first failure waits the
    base window rather than skipping it.
    """
    window = settings.publish_retry_defer_seconds * (2 ** max(0, attempts - 1))
    return float(min(window, settings.publish_retry_max_defer_seconds))


def _commit_outcome(
    db: Session, publication: Publication, record: Callable[[Publication], None]
) -> None:
    """Write a publication's outcome, even if the piece moved under the worker.

    ``Content`` carries a ``version_id_col``, so the UPDATE that
    :func:`sync_content_status` and :func:`_adopt_canonical` put on the piece
    says ``AND version = <the one this worker loaded>``. The worker loaded it
    before the platform call and commits after — and the platform call is the
    one place in Pulse that holds a row across a network round trip. Anything
    that writes the content row in that window makes the commit raise
    ``StaleDataError``: the editor's two-second autosave on a piece that is
    approved but not yet out, a headline sweep, a translation landing.

    What that lost was the *outcome*. The post was live on the platform, and
    the transaction that would have said so — ``published``, the URL, the
    external id — rolled back with the version check. ``publish_one`` does not
    catch ORM errors, so the task died with the row still ``publishing`` from
    its committed claim; ``reclaim_stuck`` later found a claim with no worker
    behind it and armed the row again, and the next worker posted the piece a
    second time. Pulse had no record of the first copy to know it was one.

    So the outcome is applied by a callback that can be run more than once.
    On a stale commit, the session is rolled back — which expires both
    instances, so the next read of either is the row as it stands now, version
    included — and the callback is applied to the current row. *record* has to
    set every field the outcome consists of, because the rollback also discards
    the ``attempts`` and ``duration_ms`` this attempt flushed before the call;
    those are re-applied here from the values the instance carried in.

    Three tries. A second collision needs a second writer to land in the
    milliseconds between the reload and the commit; a third means something
    is writing the row in a tight loop and the exception is the right answer.
    """
    attempts = publication.attempts
    duration_ms = publication.duration_ms
    for remaining in range(2, -1, -1):
        record(publication)
        publication.attempts = attempts
        publication.duration_ms = duration_ms
        try:
            db.commit()
            return
        except StaleDataError:
            db.rollback()
            if not remaining:
                raise
            logger.warning(
                "publication %s: content %s changed while the platform call was "
                "in flight; recording the outcome against the current row",
                publication.id,
                publication.content_id,
            )


def _fail(db: Session, publication: Publication, error: str, *, terminal: bool) -> None:
    """Record a failed attempt, and decide when — if ever — to try again.

    A non-terminal failure is *parked* rather than returned to ``pending``. The
    difference matters more than it looks: ``pending`` with no ``scheduled_for``
    is due immediately, so the retry budget used to be spent at the sweep
    cadence regardless of what went wrong. Three attempts inside ten minutes
    answers a blip and nothing else — a platform having a half-hour outage saw
    every attempt land inside it, and the piece went terminal while the outage
    was still going, needing a hand-retry from somebody who had no reason to be
    looking. Backing off spends the same three attempts across an hour instead.

    ``scheduled`` rather than ``pending`` because that is the status whose
    meaning already includes ``scheduled_for`` — :func:`_defer` parks
    rate-limited rows the same way, and both the beat sweep and
    ``publish_tasks.publish_one``'s claim already refuse to pick up a row before
    its time. Leaving it ``pending`` with a future time would work today and be
    a trap for the next reader.
    """
    def _record(publication: Publication) -> None:
        publication.error = clip_error(error)
        if terminal:
            publication.status = PublicationStatus.FAILED
            # Cleared, because it is now a lie. A row that failed on its last
            # attempt is carrying the ``scheduled_for`` from the retry before
            # it — a time in the future, on a row nothing will ever come back
            # for, which the calendar and the publications list both read as
            # "still to come".
            publication.scheduled_for = None
            sync_content_status(publication.content)
        else:
            publication.status = PublicationStatus.SCHEDULED
            publication.scheduled_for = utcnow() + timedelta(seconds=wait)
            if wait:
                publication.error = clip_error(f"{error} — retrying in {round(wait)}s")

    # Computed once, outside the callback: a re-applied outcome after a stale
    # commit (see :func:`_commit_outcome`) must park the row at the same time
    # the first application chose, not at a fresh ``utcnow()``.
    wait = 0.0 if terminal else retry_defer_seconds(publication.attempts)
    # Through the same door as a success, and for the same reason: a terminal
    # failure writes the content row (``sync_content_status``), and a version
    # that moved during the platform call would otherwise unwind the record of
    # the attempt — leaving the row ``publishing`` with nothing behind it, to
    # be reclaimed and charged a second time for the same failure.
    _commit_outcome(db, publication, _record)
    duration = publication.duration_ms
    logger.warning(
        "publication %s to %s failed (%s, %s): %s",
        publication.id,
        publication.platform.value,
        "terminal" if terminal else f"attempt {publication.attempts}",
        f"{duration}ms" if duration else "no duration",
        error,
    )
    if terminal:
        _notify_failed(db, publication)


def sync_content_status(content: Content) -> None:
    """Derive the content's status from its publications.

    One success is enough to call the piece published — see the module
    docstring. It only becomes ``failed`` when every platform Pulse was still
    going to try is terminal and none of them succeeded.

    **A cancelled row is not evidence of anything.** Cancelling is the user
    saying "not this platform", and the piece's status should read as though the
    row had never been armed. Counting it as terminal made the two words mean
    the same thing in opposite directions:

    * a piece whose every publication the user cancelled — which is all that
      ``DELETE /content/{id}/schedule`` does — was one call to this function
      away from being reported ``failed``, when nothing about it had failed;
    * and a piece with one failed platform and one the user then cancelled came
      out ``failed`` only because the cancel happened to be counted, which is
      the right answer reached from the wrong premise. Excluding cancelled rows
      keeps that answer: the failed row is the only one left, it is terminal,
      and no platform succeeded.

    So the verdict is taken over the rows that were still in play. When every
    row is cancelled there is nothing to derive from and the status is left
    alone — the piece is exactly as approved (or as drafted) as it was before
    anything was queued for it, which is what a caller undoing a schedule meant.

    Public because the cancelling paths have to call it too. ``execute`` and
    ``_fail`` reach a verdict by finishing a publication; ``unschedule_content``
    and the ``cancel_publication`` task reach one by *removing* the last
    publication that could still have changed it, and for a while neither said
    so. A piece with one failed platform and one scheduled elsewhere sat
    ``approved`` after the schedule was cancelled: no worker would ever touch it
    again, no sweep would look at it (``release_approved`` declines a piece that
    has publications, and ``publish_due`` sees no armed rows), and the only
    thing still claiming it was on its way was the status column.

    **``failed`` is not a ratchet.** The last arm below is what makes this a
    function of the publications rather than a one-way walk through them. Arming
    a row on a failed piece — retrying one platform, re-publishing to another —
    makes "every platform gave up" untrue again, and until the piece said so a
    user who clicked Retry watched a worker publish a piece the UI went on
    calling failed, right up until it succeeded. ``draft`` and ``review`` are
    left alone: those are states nothing has been queued from yet, and a piece
    is not approved just because a row exists.
    """
    publications = [
        p for p in content.publications if p.status != PublicationStatus.CANCELLED
    ]
    if not publications:
        return

    if any(p.status == PublicationStatus.PUBLISHED for p in publications):
        content.status = ContentStatus.PUBLISHED
        if content.published_at is None:
            content.published_at = utcnow()
    elif all(p.is_terminal for p in publications):
        content.status = ContentStatus.FAILED
    elif content.status == ContentStatus.FAILED:
        content.status = ContentStatus.APPROVED


def reclaim_stuck(db: Session, *, now: datetime | None = None) -> int:
    """Re-arm publications abandoned mid-publish. Returns how many. Commits.

    ``publish_tasks.publish_one`` claims a row by moving it to ``publishing``
    and committing before it calls :func:`execute`. That is what stops two
    workers publishing the same row — and it is also a one-way door if the
    worker never comes back. A process killed between the claim and the outcome
    (OOM, a deploy restarting the service, SIGKILL) leaves the row ``publishing``
    with nothing running: :func:`due_publications` only looks at ``pending`` and
    ``scheduled``, and Celery's redelivery of the task finds the row already
    claimed and skips it. The publication is then stuck forever, with no error
    on it to say so.

    The cutoff is what keeps this from double-posting. ``publish_one``'s hard
    time limit is under three minutes and its soft limit already returns the row
    to ``pending``, so a row untouched for ``publish_stuck_after_seconds`` has no
    live task behind it. ``updated_at`` is the clock — it moves on the claim, so
    it measures the age of *this* claim rather than of the row.

    The attempt is counted *here*, and it has to be: it is genuinely spent, and
    a publish that reliably kills its worker should exhaust its retries and be
    looked at by a human rather than cycling forever.

    Counting it is this function's job because the increment :func:`execute`
    does not survive the death this function is cleaning up after. ``execute``
    does ``attempts += 1`` and *flushes* — it does not commit, and nothing
    between that flush and the adapter call commits either, since
    ``_credentials_for`` only reads and the adapter never sees the session. The
    transaction is still open when the worker is killed, so the increment rolls
    back with it and the row arrives here carrying the count from *before* the
    attempt that did the killing. Reading that count and re-arming on it was a
    free retry, which made a payload that segfaults its worker every time an
    infinite loop: claim, die, roll back, re-arm at the same number, claim
    again — and never an error anybody would be shown, because the row looked
    busy the whole way round. A row in this query is proof the attempt happened;
    only a commit inside ``_fail``, ``_defer`` or the success path moves a row
    out of ``publishing``, and each of those persists its own increment.
    """
    cutoff = (now or utcnow()) - timedelta(seconds=settings.publish_stuck_after_seconds)
    stuck = list(
        db.scalars(
            select(Publication)
            .where(
                Publication.status == PublicationStatus.PUBLISHING,
                Publication.updated_at <= cutoff,
            )
            # The row that spends its retries here calls ``sync_content_status``,
            # which walks ``publication.content`` and then that content's own
            # publications. Both hops were lazy, so a sweep after a worker died
            # mid-batch paid two SELECTs per burned row — and the moment this
            # sweep has work to do is exactly the moment something is already
            # wrong. ``Content.publications`` is ``lazy="selectin"``, so loading
            # the content eagerly brings the sibling publications with it in one
            # more query for the whole batch rather than one per row.
            .options(
                joinedload(Publication.content).selectinload(Content.publications)
            )
        )
    )
    # The notices for the rows this sweep finishes off — built here, sent after
    # the commit below. Both halves of that are deliberate.
    #
    # Sent after, for the ordering ``_fail`` and ``_notify_published`` both keep:
    # an endpoint that turns round and reads the API back must find what the
    # payload describes.
    #
    # Built before, because the commit expires every row it touches. Holding the
    # publications and reading them afterwards costs a re-fetch of each piece and
    # its project — and, through ``Content.publications``, its sibling rows —
    # one burned row at a time, which is the per-row cost this function's own
    # eager load exists to avoid, arriving one statement later. Worse, a user
    # with a webhook configured pays it *per notice*, since ``webhooks.emit``
    # commits: the first notice would expire the rows the rest were waiting to
    # be read from. A plain list of bodies is immune to both.
    # See tests.test_reclaim_stuck_budget.
    notices: list[tuple[int, dict]] = []
    for publication in stuck:
        publication.attempts += 1
        logger.warning(
            "publication %s to %s was left mid-publish; re-arming (attempt %d)",
            publication.id,
            publication.platform.value,
            publication.attempts,
        )
        if publication.attempts >= settings.publish_max_retries:
            publication.status = PublicationStatus.FAILED
            # Cleared for the reason ``_fail`` clears it: a terminal row is one
            # nothing will ever come back for, and every reader of the column
            # treats a non-null value as a plan. A row deferred by an earlier
            # retry carries that ``scheduled_for`` through the claim — ``execute``
            # moves the row to ``publishing`` without clearing it — so the row
            # arriving here is holding the time of a retry that has already been
            # spent. Left set, it put a publication that is finished and failed
            # on the calendar as a planned post, and sorted it into the "still to
            # come" half of ``GET /content/queue/publications``, whose ordering
            # is written around every failed row having a null one.
            publication.scheduled_for = None
            publication.error = (
                "The worker publishing this stopped before it finished, and the "
                "retries are spent. Retry it by hand once the cause is known."
            )
            sync_content_status(publication.content)
            notice = _failure_notice(publication)
            if notice is not None:
                notices.append(notice)
        else:
            publication.status = PublicationStatus.PENDING
            publication.error = (
                "The worker publishing this stopped before it finished — retrying."
            )
    if stuck:
        db.commit()
    # The other terminal path — ``_fail`` — fires this, and a publication that
    # burns its last attempt by killing its worker is no less finished than one
    # that burns it on a refusal. It was the quieter of the two and the one more
    # likely to need a human: nothing was returned to blame, so the only trace
    # was a warning in the worker log, and the user's ``publication.failed``
    # subscription never heard about it at all.
    for user_id, data in notices:
        webhooks.emit(
            db,
            user_id=user_id,
            event=WebhookEvent.PUBLICATION_FAILED,
            data=data,
        )
    return len(stuck)


def due_publications(db: Session, *, now: datetime | None = None) -> list[Publication]:
    """Publications a worker should pick up right now.

    Covers both "publish immediately" (``pending``) and "scheduled, and the time
    has come". Rows that have burned their retries are excluded by status.
    """
    moment = now or utcnow()
    return list(
        db.scalars(
            select(Publication).where(
                Publication.status.in_(
                    [PublicationStatus.PENDING, PublicationStatus.SCHEDULED]
                ),
                (Publication.scheduled_for.is_(None))
                | (Publication.scheduled_for <= moment),
            )
        )
    )


#: The key a platform's rate limit applies to. A limit is enforced against the
#: credential that made the request, so two users publishing to Dev.to have
#: their own budgets and must not stand each other down.
RateLimitKey = tuple[int, Platform]


def collect_metrics(
    db: Session,
    publication: Publication,
    *,
    user_id: int | None = None,
    rate_limited: set[RateLimitKey] | None = None,
) -> ContentMetric | None:
    """Poll one published post for engagement. Returns the new row, or ``None``.

    Quiet about failure on purpose: metrics are a nice-to-have, and a platform
    having a bad day should not fill the log with errors or mark anything
    invalid.

    *user_id* is whose credentials to poll with. It is optional because the
    single-post refresh button has one publication and no reason to know, and
    walking ``publication.content.project`` answers it in two queries. The sweep
    passes it because it does have a reason: it commits after every row it
    records, which expires the session, so that walk would re-read a whole
    ``Content`` — article body included — once per publication on the install.

    *rate_limited* is the sweep's memory, and it is what stops the poller
    answering a 429 by making the next request. The numbers are cumulative
    counters read once every few hours, so a platform that has just said "stop"
    has nothing to tell us that waiting for the next sweep would lose — while a
    caller that keeps going makes one refused request per post on that account,
    which is how a rate limit becomes a block. Pass a set to
    :func:`app.tasks.metrics_tasks.collect_all_metrics`'s loop and every
    publication sharing the refused (user, platform) is skipped without a
    request. Omit it and each call stands alone, which is what the single-post
    refresh button wants.
    """
    if publication.status != PublicationStatus.PUBLISHED or not publication.external_id:
        return None

    adapter = publishers.get_adapter(publication.platform)
    if not adapter.supports_metrics:
        return None

    owner_id = user_id if user_id is not None else publication.content.project.user_id
    key: RateLimitKey = (owner_id, publication.platform)
    if rate_limited is not None and key in rate_limited:
        logger.debug(
            "metrics poll for publication %s skipped: %s is rate-limiting this "
            "account for the rest of the sweep",
            publication.id,
            publication.platform.value,
        )
        return None

    try:
        credentials = _credentials_for(db, owner_id, publication.platform)
    except PublishError as exc:
        # ``NotConnected`` or a ``CredentialError`` from decryption — nothing
        # was shown to a platform yet, so there is nothing to redact.
        logger.info("metrics poll for publication %s skipped: %s", publication.id, exc)
        return None

    try:
        snapshot = adapter.fetch_metrics(publication.external_id, credentials)
    except RateLimited as exc:
        # The same backstop :func:`execute` applies before *its* failure is
        # written or logged. A platform that echoes the token it was shown
        # (``test_a_platform_that_echoes_the_token_does_not_get_it_stored``)
        # echoes it on a metrics call as readily as on a publish, and this
        # line is the one place in the poll where what it said reaches a log
        # that is shipped off the box.
        _redact_credentials(exc, adapter, credentials)
        if rate_limited is not None:
            rate_limited.add(key)
        logger.info(
            "metrics poll for publication %s hit %s's rate limit; standing down "
            "for this account until the next sweep: %s",
            publication.id,
            publication.platform.value,
            exc,
        )
        return None
    except PublishError as exc:
        _redact_credentials(exc, adapter, credentials)
        logger.info("metrics poll for publication %s skipped: %s", publication.id, exc)
        return None

    metric = ContentMetric(
        publication_id=publication.id,
        views=snapshot.views,
        reads=snapshot.reads,
        clicks=snapshot.clicks,
        reactions=snapshot.reactions,
        comments=snapshot.comments,
        shares=snapshot.shares,
    )
    db.add(metric)
    db.commit()
    return metric


__all__ = [
    "TIMEOUT_ERROR",
    "NotConnected",
    "RateLimitKey",
    "build_request",
    "collect_metrics",
    "due_publications",
    "execute",
    "queue",
    "reclaim_stuck",
    "record_timeout",
]
