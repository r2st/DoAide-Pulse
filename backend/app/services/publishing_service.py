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
from collections.abc import Sequence
from datetime import datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models.content import CANONICAL_URL_MAX_LENGTH, Content, ContentStatus
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import (
    EXTERNAL_ID_MAX_LENGTH,
    EXTERNAL_URL_MAX_LENGTH,
    Platform,
    Publication,
    PublicationStatus,
)
from app.models.webhook import WebhookEvent
from app.services import publishers, utm, webhook_payloads, webhooks
from app.services.crypto import CredentialEncryptionError, decrypt_credentials
from app.services.errors import clip_error
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
        elif publication.status == PublicationStatus.PUBLISHED:
            # Already live. Re-queueing would double-post.
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


def cancel_armed(db: Session, content: Content) -> list[Publication]:
    """Take *content* off the queue: cancel what has not run, drop its date.

    Archiving a piece is the user saying it is not going out, and it was the one
    way of saying that which nothing acted on. ``DELETE /content/{id}/schedule``
    cancels the rows; archiving set a column, left the schedule armed, and the
    beat sweep published the piece days later — so the review queue's Reject
    button posted the thing that had just been rejected.

    Terminal rows are left alone. Anything already live stays live: Herald
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
        raise NotConnected(
            f"No live {platform.value} connection — add one in Settings."
        )
    try:
        return decrypt_credentials(connection.encrypted_credentials)
    except CredentialEncryptionError as exc:
        # A key change is a credential problem, not a transient one.
        raise CredentialError(str(exc)) from exc


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
    content: Content, *, platform: Platform | None = None, as_draft: bool = False
) -> PublishRequest:
    """The flat value object adapters take, built from a content row.

    Called while the session is open so the ``project`` relationship is loaded
    before the worker touches it.

    *platform* is what makes the request platform-specific without making the
    adapters stateful: it supplies the campaign source for the outbound links
    and the idempotency key. It is optional so that callers who only want to
    preview the shared parts — the SEO panel, tests — need not name one.
    """
    project = content.project
    tagged = _Campaign(content, platform)

    return PublishRequest(
        title=content.title,
        body_markdown=tagged.markdown(content.body_markdown),
        excerpt=content.excerpt,
        meta_description=content.meta_description,
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

    user_id = content.project.user_id
    adapter = publishers.get_adapter(publication.platform)

    publication.status = PublicationStatus.PUBLISHING
    publication.attempts += 1
    db.flush()

    try:
        credentials = _credentials_for(db, user_id, publication.platform)
        result = adapter.publish(
            build_request(
                content,
                platform=publication.platform,
                as_draft=publication.as_draft,
            ),
            credentials,
        )
    except (NotConnected, NotImplementedAdapter, UnsupportedOption) as exc:
        # None of these is transient: no amount of retrying connects an account,
        # finishes an adapter, or gives a platform a feature it does not have.
        _fail(db, publication, str(exc), terminal=True)
        return publication
    except CredentialError as exc:
        _mark_connection_invalid(db, user_id, publication.platform, str(exc))
        _fail(db, publication, str(exc), terminal=True)
        return publication
    except RateLimited as exc:
        # The platform said when to come back, and coming back sooner is how a
        # soft limit becomes a ban. Park the row until then rather than leaving
        # it `pending` for the next sweep, which is minutes away at most.
        _defer(db, publication, exc)
        return publication
    except PublishError as exc:
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

    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = utcnow()
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
    publication.error = None

    _adopt_canonical(content, publication, result)
    # Captured before the sync, because the interesting moment is the
    # *transition* — a piece already live on Dev.to going out on Mastodon is
    # not news, and firing "published" per platform would make the event mean
    # something different from its name.
    was_published = content.status == ContentStatus.PUBLISHED
    sync_content_status(content)
    db.commit()
    if not was_published and content.status == ContentStatus.PUBLISHED:
        _notify_published(db, content, publication)
    logger.info(
        "published content %s to %s: %s",
        content.id,
        publication.platform.value,
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


def _notify_failed(db: Session, publication: Publication) -> None:
    """Tell this user's webhooks that a platform gave up on a piece.

    Only for terminal failures. A mid-retry blip is not something to page
    anyone about, and an endpoint told about all three attempts learns nothing
    it did not know after the third.
    """
    content = publication.content
    project = content.project if content else None
    if project is None:  # pragma: no cover - a publication always has a project
        return
    webhooks.emit(
        db,
        user_id=project.user_id,
        event=WebhookEvent.PUBLICATION_FAILED,
        data={
            "content": webhook_payloads.content_payload(content),
            "publication": webhook_payloads.publication_payload(publication),
        },
    )


def _adopt_canonical(
    content: Content, publication: Publication, result: PublishResult
) -> None:
    """Adopt this publication's URL as the piece's canonical, when it should be.

    Herald's syndication model has always depended on ``canonical_url`` — every
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
    # platform and Herald still believes it is not. Declining to adopt is the
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
    publication.error = clip_error(error)
    if terminal:
        publication.status = PublicationStatus.FAILED
        # Cleared, because it is now a lie. A row that failed on its last
        # attempt is carrying the ``scheduled_for`` from the retry before it —
        # a time in the future, on a row nothing will ever come back for, which
        # the calendar and the publications list both read as "still to come".
        publication.scheduled_for = None
        sync_content_status(publication.content)
    else:
        wait = retry_defer_seconds(publication.attempts)
        publication.status = PublicationStatus.SCHEDULED
        publication.scheduled_for = utcnow() + timedelta(seconds=wait)
        if wait:
            publication.error = clip_error(f"{error} — retrying in {round(wait)}s")
    db.commit()
    logger.warning(
        "publication %s to %s failed (%s): %s",
        publication.id,
        publication.platform.value,
        "terminal" if terminal else f"attempt {publication.attempts}",
        error,
    )
    if terminal:
        _notify_failed(db, publication)


def sync_content_status(content: Content) -> None:
    """Derive the content's status from its publications.

    One success is enough to call the piece published — see the module
    docstring. It only becomes ``failed`` when every platform Herald was still
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
            publication.error = (
                "The worker publishing this stopped before it finished, and the "
                "retries are spent. Retry it by hand once the cause is known."
            )
            sync_content_status(publication.content)
        else:
            publication.status = PublicationStatus.PENDING
            publication.error = (
                "The worker publishing this stopped before it finished — retrying."
            )
    if stuck:
        db.commit()
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
        snapshot = adapter.fetch_metrics(publication.external_id, credentials)
    except RateLimited as exc:
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
    except (PublishError, NotConnected) as exc:
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
