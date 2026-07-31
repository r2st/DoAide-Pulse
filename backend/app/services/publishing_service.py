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
    platforms: list[Platform | str],
    *,
    scheduled_for: datetime | None = None,
    as_draft: bool = False,
) -> list[Publication]:
    """Create (or re-arm) a publication per platform. Does not publish.

    Re-queueing a platform that previously failed resets it rather than adding a
    second row — the unique constraint on (content, platform) makes that the
    only correct behaviour, and "retry this one" is the common case.

    When the project names a canonical platform and this batch contains both it
    and somewhere else to syndicate to, the copies are held back — see
    :func:`_syndication_schedule`.
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
    # head start even when the delay is switched off.
    canonical = _canonical_platform(content)
    out.sort(key=lambda p: (p.platform != canonical, p.platform.value))
    return out


def _canonical_platform(content: Content) -> Platform | None:
    """The platform whose URL this project treats as the original, if any."""
    project = content.project
    if project is None or not project.auto_canonical:
        return None
    return project.canonical_platform


def _syndication_schedule(
    content: Content,
    platforms: list[Platform | str],
    *,
    base: datetime | None,
) -> dict[Platform, datetime | None]:
    """When each platform in this batch should go out.

    Publishing the original and its copies in the same instant loses the point
    of a canonical URL twice over: the copies are dispatched before the original
    has an ``external_url`` to be canonical *to*, and a crawler has no reason to
    believe the original came first. So when the project designates a canonical
    platform and this batch also contains somewhere else, the rest wait
    ``syndication_delay_seconds`` behind it.

    Returns a mapping for the platforms whose time differs from *base*; anything
    absent keeps the caller's own schedule. An empty dict means "nothing to
    stagger", which covers the ordinary single-platform publish.
    """
    canonical = _canonical_platform(content)
    delay = settings.syndication_delay_seconds
    if canonical is None or delay <= 0:
        return {}

    wanted = {p if isinstance(p, Platform) else Platform(p) for p in platforms}
    # Nothing to order: the original is not in this batch (so its URL either
    # already exists or is not coming), or there is nothing to syndicate.
    if canonical not in wanted or len(wanted) < 2:
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
        connection.last_error = error


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
                "content": content.slug,
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

    Four things stop it firing, and each is a case where guessing would be worse
    than leaving the field empty:

    * the project opted out, or a human already typed a canonical URL — an
      explicit answer beats an inferred one;
    * the project names a canonical platform and this is not it, so this URL is
      itself a copy;
    * the post was staged as a draft, whose URL is a private editor link that
      would 404 for a crawler;
    * the platform did not return an absolute ``http(s)`` URL to use.
    """
    project = content.project
    if project is None or not project.auto_canonical or content.canonical_url:
        return
    if project.canonical_platform and publication.platform != project.canonical_platform:
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
    publication.error = f"{exc} — retrying in {round(wait)}s"
    db.commit()
    logger.info(
        "publication %s to %s rate-limited (attempt %d); deferred %.0fs",
        publication.id,
        publication.platform.value,
        publication.attempts,
        wait,
    )


def _fail(db: Session, publication: Publication, error: str, *, terminal: bool) -> None:
    publication.error = error
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


def collect_metrics(db: Session, publication: Publication) -> ContentMetric | None:
    """Poll one published post for engagement. Returns the new row, or ``None``.

    Quiet about failure on purpose: metrics are a nice-to-have, and a platform
    having a bad day should not fill the log with errors or mark anything
    invalid.
    """
    if publication.status != PublicationStatus.PUBLISHED or not publication.external_id:
        return None

    adapter = publishers.get_adapter(publication.platform)
    if not adapter.supports_metrics:
        return None

    try:
        credentials = _credentials_for(
            db, publication.content.project.user_id, publication.platform
        )
        snapshot = adapter.fetch_metrics(publication.external_id, credentials)
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
    "build_request",
    "collect_metrics",
    "due_publications",
    "execute",
    "queue",
]
