"""Making a headline swap reach the reader.

:func:`app.services.headlines.apply_headline` changes Pulse's copy of a
title. That is half the operation. The other half — telling the destinations
that are already carrying the piece — is this module, and without it the whole
headline feature measures something that never happened: readers keep seeing
the headline the post went out with, while the engagement they generate is
credited to the new one that only exists in Pulse's database.

Not every destination can be told, and pretending otherwise is the failure
mode worth naming. A Bluesky post has no title. A sent Buttondown issue is in
inboxes. Medium's API has no update. Four destinations can be told —
Dev.to, Hashnode, WordPress and a Git repo — and they declare it with
``Adapter.supports_title_update``. What this module returns is a per-
publication account of which were reached, which cannot be, and which failed,
so that :func:`app.services.headlines.performance` can exclude the ones that
are not carrying the headline it is about to credit.

Deliberately not retried. A retitle that fails leaves ``live_title`` at the old
headline, which is *true* — the post still says that — and the piece is simply
excluded from headline attribution until a later swap or a manual re-run
catches it up. A retry queue here would be a second publishing pipeline for an
operation whose failure costs nothing but a stale headline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishers
from app.services.errors import clip_error, sanitize_unexpected_error
from app.services.publishers.base import PublishError

# Named imports rather than `publishing_service.<private>` at each call site, so
# the coupling is stated once and in one place. These are the publish path's own
# internals and this module is a second write to the same platform with the same
# credentials and the same text-selection rules — reimplementing any of the
# three here would give a retitle its own opinion about which translation a
# destination gets, which is exactly the disagreement `build_request` exists to
# make impossible.
from app.services.publishing_service import (
    _credentials_for,
    _redact_credentials,
    _translation_for,
    build_request,
)

logger = logging.getLogger(__name__)

#: The destination now shows the piece's current headline.
UPDATED = "updated"
#: It already did — nothing was sent.
UNCHANGED = "unchanged"
#: This platform's API cannot change the title of a live post.
UNSUPPORTED = "unsupported"
#: It can, and the attempt failed. ``live_title`` is left as it was.
FAILED = "failed"
#: A draft, a cancelled row, or one with no platform id to address.
SKIPPED = "skipped"


@dataclass(frozen=True)
class SyncOutcome:
    """What happened to one publication when the headline changed."""

    publication_id: int
    platform: Platform
    status: str
    detail: str = ""

    @property
    def reached(self) -> bool:
        """Whether the destination is showing the piece's current headline."""
        return self.status in {UPDATED, UNCHANGED}

    def as_dict(self) -> dict:
        """The wire shape, for a task return value and a log line."""
        return {
            "publication_id": self.publication_id,
            "platform": self.platform.value,
            "status": self.status,
            "detail": self.detail,
        }


def carries_title_changes(platform: Platform) -> bool:
    """Whether a live post on *platform* can be retitled at all.

    An unregistered or unimplemented platform answers False, which is the safe
    direction: it means "assume the reader is not seeing the new headline".
    """
    try:
        adapter = publishers.get_adapter(platform)
    except Exception:  # pragma: no cover - registry is exhaustive over Platform
        return False
    return bool(adapter.implemented and adapter.supports_title_update)


def is_tracking(publication: Publication, title: str) -> bool:
    """Whether *publication* shows *title* to its readers.

    The question :func:`app.services.headlines.performance` asks before
    counting a publication's engagement as evidence about a headline.

    ``live_title`` is NULL for rows published before the column existed. Those
    read as tracking, which is how they were already being counted; the
    platform capability is the guard that does the real work for them, and it
    needs no backfill.
    """
    if not carries_title_changes(publication.platform):
        return False
    return publication.live_title is None or publication.live_title == title


def _syncable(db: Session, content: Content) -> list[Publication]:
    return list(
        db.scalars(
            select(Publication)
            .where(
                Publication.content_id == content.id,
                Publication.status == PublicationStatus.PUBLISHED,
            )
            .order_by(Publication.id)
        )
    )


def sync_title(db: Session, content: Content) -> list[SyncOutcome]:
    """Push *content*'s current headline to every destination that can take it.

    Commits its own work, one publication at a time: a Dev.to retitle that
    succeeded is a fact about the world whether or not the WordPress one after
    it does, and holding both in a transaction until the end would lose the
    first to the second's rollback.

    Never raises. Every failure becomes a :class:`SyncOutcome` — the caller is
    a beat sweep or an endpoint that has already changed the title, and neither
    can undo that.
    """
    outcomes: list[SyncOutcome] = []
    try:
        user_id = content.project.user_id

        for publication in _syncable(db, content):
            outcome = _sync_one(db, content, publication, user_id)
            outcomes.append(outcome)
            if outcome.status == UPDATED:
                db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("headline sync crashed for content %s", content.id)
        outcomes.append(
            SyncOutcome(
                publication_id=0,
                platform=Platform.DEVTO,
                status=FAILED,
                detail=f"Sync crashed: {sanitize_unexpected_error(exc)}",
            )
        )

    return outcomes


def _sync_one(
    db: Session, content: Content, publication: Publication, user_id: int
) -> SyncOutcome:
    platform = publication.platform

    if publication.as_draft or not publication.external_id:
        return SyncOutcome(
            publication.id,
            platform,
            SKIPPED,
            "Nothing live to retitle." if not publication.external_id else "Still a draft.",
        )

    if not carries_title_changes(platform):
        return SyncOutcome(
            publication.id,
            platform,
            UNSUPPORTED,
            f"{platform.value} cannot change the title of a post that is already out.",
        )

    # The title this destination would be published with *today*, which is the
    # translated one where a translation is going out — a French post is not
    # retitled with an English headline. Built through the same function the
    # publish path uses so the two can never disagree about which text a
    # destination gets.
    choice = _translation_for(db, user_id, platform, content)
    request = build_request(
        content,
        platform=platform,
        as_draft=publication.as_draft,
        translation=choice.translation,
    )

    if publication.live_title == request.title:
        return SyncOutcome(publication.id, platform, UNCHANGED, "Already showing it.")

    adapter = publishers.get_adapter(platform)
    try:
        credentials = _credentials_for(db, user_id, platform)
    except PublishError as exc:
        # The platform was disconnected or the stored secret could not be
        # decrypted — both arrive as a PublishError subclass with a user-safe
        # message. The outcome is returned to a beat sweep that counts it and
        # moves on, so without a line the whole sweep failing this way looked
        # exactly like a sweep that found nothing to retitle.
        logger.warning(
            "headline sync for publication %s on %s could not read credentials: %s",
            publication.id,
            platform.value,
            exc,
        )
        return SyncOutcome(publication.id, platform, FAILED, clip_error(str(exc)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "headline sync for publication %s on %s: unexpected credential error",
            publication.id,
            platform.value,
        )
        return SyncOutcome(publication.id, platform, FAILED, sanitize_unexpected_error(exc))

    try:
        adapter.update_title(request, credentials, publication.external_id)
    except PublishError as exc:
        _redact_credentials(exc, adapter, credentials)
        logger.warning(
            "headline sync failed for publication %s on %s: %s",
            publication.id,
            platform.value,
            exc,
        )
        return SyncOutcome(publication.id, platform, FAILED, clip_error(str(exc)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("headline sync errored for publication %s", publication.id)
        return SyncOutcome(publication.id, platform, FAILED, sanitize_unexpected_error(exc))

    publication.live_title = request.title[:300]
    logger.info(
        "publication %s on %s retitled to %r",
        publication.id,
        platform.value,
        request.title,
    )
    return SyncOutcome(publication.id, platform, UPDATED, "")


__all__ = [
    "FAILED",
    "SKIPPED",
    "UNCHANGED",
    "UNSUPPORTED",
    "UPDATED",
    "SyncOutcome",
    "carries_title_changes",
    "is_tracking",
    "sync_title",
]
