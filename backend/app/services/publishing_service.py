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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content, ContentStatus
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import WebhookEvent
from app.services import publishers, utm, webhook_payloads, webhooks
from app.services.crypto import CredentialEncryptionError, decrypt_credentials
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
    """
    existing = {p.platform: p for p in content.publications}
    out: list[Publication] = []
    schedule = _syndication_schedule(content, platforms, base=scheduled_for)

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
    # Canonical first, so a caller that dispatches in order gives the original a
    # head start even when the delay is switched off. Then the destinations that
    # host the article, ahead of the ones that only carry a link to it: when no
    # canonical platform is named, whichever publishes first becomes the piece's
    # canonical, and a plain alphabetical order handed that to Bluesky.
    canonical = _canonical_platform(content)
    out.sort(key=lambda p: (p.platform != canonical, *_original_rank(p.platform)))
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
        connection.last_error = _clip(error)


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
        return utm.tag(url, **self.params) if self.active else url

    def markdown(self, body: str) -> str:
        """Tag the body's links to the project's own site, and nothing else."""
        if not self.active:
            return body
        return utm.tag_markdown_links(body, host=self.host, **self.params)


def execute(db: Session, publication: Publication) -> Publication:
    """Attempt one publication, recording the outcome. Never raises.

    Retryable failures leave the row ``pending`` until ``publish_max_retries``
    is spent; credential failures and unfinished adapters go straight to
    ``failed``, because retrying either is guaranteed to fail the same way.
    """
    content = publication.content
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
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected error publishing %s", publication.id)
        _fail(db, publication, f"Unexpected error: {exc}", terminal=True)
        return publication

    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = utcnow()
    publication.external_id = result.external_id
    publication.external_url = result.external_url
    publication.error = None

    _adopt_canonical(content, publication, result)
    # Captured before the sync, because the interesting moment is the
    # *transition* — a piece already live on Dev.to going out on Mastodon is
    # not news, and firing "published" per platform would make the event mean
    # something different from its name.
    was_published = content.status == ContentStatus.PUBLISHED
    _sync_content_status(content)
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
    * the platform did not return an absolute ``http(s)`` URL to use.
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
        wait = float(settings.publish_scan_interval_seconds)
    wait = min(wait, float(settings.publish_rate_limit_max_defer_seconds))

    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = utcnow() + timedelta(seconds=wait)
    publication.error = _clip(f"{exc} — retrying in {round(wait)}s")
    db.commit()
    logger.info(
        "publication %s to %s rate-limited (attempt %d); deferred %.0fs",
        publication.id,
        publication.platform.value,
        publication.attempts,
        wait,
    )


#: How much of a failure message is worth keeping on the row.
#:
#: Adapter messages quote what the platform said, and several of them quote the
#: whole body when it is not the shape they expected — ``f"Hashnode returned no
#: post: {data}"``. That body is not ours and has no size limit; ``error`` is a
#: ``Text`` column with none either, and it is written again on every attempt
#: and rendered in the publications list. A megabyte of someone else's JSON in
#: a field the UI shows is a bad row and a slow page, and the part that says
#: what went wrong is in the first line regardless.
MAX_ERROR_CHARS = 2000


def _clip(error: str) -> str:
    """A failure message bounded to :data:`MAX_ERROR_CHARS`.

    Applied at the one place every failure funnels through rather than in each
    adapter, so a new adapter cannot forget it.
    """
    if len(error) <= MAX_ERROR_CHARS:
        return error
    return error[: MAX_ERROR_CHARS - 1].rstrip() + "…"


def _fail(db: Session, publication: Publication, error: str, *, terminal: bool) -> None:
    publication.error = _clip(error)
    publication.status = (
        PublicationStatus.FAILED if terminal else PublicationStatus.PENDING
    )
    if terminal:
        _sync_content_status(publication.content)
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


def _sync_content_status(content: Content) -> None:
    """Derive the content's status from its publications.

    One success is enough to call the piece published — see the module
    docstring. It only becomes ``failed`` when every platform is terminal and
    none succeeded.
    """
    publications = content.publications
    if not publications:
        return

    if any(p.status == PublicationStatus.PUBLISHED for p in publications):
        content.status = ContentStatus.PUBLISHED
        if content.published_at is None:
            content.published_at = utcnow()
    elif all(p.is_terminal for p in publications):
        content.status = ContentStatus.FAILED


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

    The attempt is counted: it is genuinely spent, and a publish that reliably
    kills its worker should exhaust its retries and be looked at by a human
    rather than cycling forever.
    """
    cutoff = (now or utcnow()) - timedelta(seconds=settings.publish_stuck_after_seconds)
    stuck = list(
        db.scalars(
            select(Publication).where(
                Publication.status == PublicationStatus.PUBLISHING,
                Publication.updated_at <= cutoff,
            )
        )
    )
    for publication in stuck:
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
            _sync_content_status(publication.content)
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
    rate_limited: set[RateLimitKey] | None = None,
) -> ContentMetric | None:
    """Poll one published post for engagement. Returns the new row, or ``None``.

    Quiet about failure on purpose: metrics are a nice-to-have, and a platform
    having a bad day should not fill the log with errors or mark anything
    invalid.

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

    user_id = publication.content.project.user_id
    key: RateLimitKey = (user_id, publication.platform)
    if rate_limited is not None and key in rate_limited:
        logger.debug(
            "metrics poll for publication %s skipped: %s is rate-limiting this "
            "account for the rest of the sweep",
            publication.id,
            publication.platform.value,
        )
        return None

    try:
        credentials = _credentials_for(db, user_id, publication.platform)
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
    "NotConnected",
    "RateLimitKey",
    "build_request",
    "collect_metrics",
    "due_publications",
    "execute",
    "queue",
    "reclaim_stuck",
]
