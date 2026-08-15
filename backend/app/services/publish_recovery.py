"""Re-arming publications whose reason for failing has since stopped being true.

Herald's retry story is otherwise complete, and deliberately so. A blip is
retried in-process by :meth:`app.services.publishers.base.Adapter._request`; a
failure that outlives that is parked by :func:`app.services.publishing_service._fail`
and re-armed by the beat sweep until ``publish_max_retries`` is spent; a rate
limit is parked until the platform's own ``Retry-After``. Every one of those is
about a failure that *might not recur on its own*, and the answer in each case
is to wait and ask again.

This module is for the other kind. ``NotConnected`` is permanent when it is
raised and ``execute`` is right to end the row on it — no amount of retrying
connects an account. But it is permanent *given a state*, and the state is one a
person changes later, somewhere else entirely: they open Settings and connect
Dev.to. Nothing joined those two facts up. The publication stayed ``failed``,
the piece stayed ``failed``, and the only route back was a human noticing and
clicking Retry once per platform per piece — for work Herald had already written,
approved and queued.

That gap is what put ten pieces in ``failed`` on production. Every one of them
belonged to an account with no connections at all, and every one of them was
armed before :func:`app.services.content_pipeline.publishable_destinations`
learned to drop a destination its owner had never connected. The arming bug is
fixed; these rows are its residue, and they are exactly the rows this recovers.

**Only the failure whose cure is recorded elsewhere.** The sweep matches the one
sentence :func:`app.services.publishing_service.not_connected_error` writes, and
re-arms only when the connection it names is now live. A credential the platform
rejected is not recovered here even though it looks similar: ``CredentialError``
marks the connection ``invalid``, and reconnecting is what flips it back to
``connected`` — which this sweep then sees, because the row it left behind says
something different and is left alone. Widening the match to "any terminal
failure" would replay content a platform refused on its merits, once per sweep,
forever.

**Idempotent, and safe to run on a schedule.** A row it re-arms is no longer
``failed``, so the next pass does not see it; a row it re-arms and which fails
the same way again gets the same sentence written back, which is a state this
will pick up again only if the connection is live — which it now is, so the
second failure is a real one and comes from the platform, not from the absence
of a connection.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.content import Content, ContentStatus
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.services import publishing_service

logger = logging.getLogger(__name__)

#: How many rows one pass will re-arm. A bound rather than a target: the sweep
#: runs on a timer beside the publish sweep, and a user who connects an account
#: after a month away could otherwise hand a single tick several hundred
#: publications and the dispatch storm that goes with them. The remainder is
#: taken by the next tick, which is minutes away.
DEFAULT_LIMIT = 50


def recoverable(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[Publication]:
    """Failed publications that a now-live connection has un-blocked.

    The match is on the *pair*: the row's error is the sentence
    :func:`app.services.publishing_service.not_connected_error` writes for that
    row's own platform, and a ``connected`` connection for that same platform
    and that row's owner exists now. Either half alone is not evidence —
    a Bluesky row carrying the Dev.to sentence is a row nothing wrote, and a
    live connection says nothing about a piece that failed for another reason.

    Archived pieces and switched-off accounts are excluded here rather than left
    to :func:`app.services.publishing_service.execute`'s last gate. That gate
    would cancel them correctly, but only after a worker has claimed the row and
    a dispatch has been spent on it — and it would rewrite a ``failed`` row to
    ``cancelled``, which is a state change nobody asked for on a piece somebody
    deliberately withdrew.
    """
    rows = db.scalars(
        select(Publication)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .join(User, User.id == Project.user_id)
        .join(
            PlatformConnection,
            (PlatformConnection.user_id == Project.user_id)
            & (PlatformConnection.platform == Publication.platform),
        )
        .where(
            Publication.status == PublicationStatus.FAILED,
            PlatformConnection.status == ConnectionStatus.CONNECTED,
            Content.status != ContentStatus.ARCHIVED,
            Project.is_active.is_(True),
            User.is_active.is_(True),
        )
        # ``execute`` walks publication → content → project → user, and
        # ``retry_hold`` walks the piece's sibling publications. Both are on the
        # path of every row this returns, so the joins above are made to carry
        # their rows rather than being repeated one lazy load at a time.
        .options(
            joinedload(Publication.content)
            .joinedload(Content.project)
            .joinedload(Project.user),
            joinedload(Publication.content).joinedload(Content.publications),
        )
        # Oldest first: a queue that has been waiting is served before one that
        # has just arrived, and the ``limit`` below then bites on the newest
        # rather than on an arbitrary slice.
        .order_by(Publication.updated_at, Publication.id)
    )
    out: list[Publication] = []
    for publication in rows.unique():
        if publication.error != publishing_service.not_connected_error(
            publication.platform
        ):
            continue
        out.append(publication)
        if len(out) >= limit:
            break
    return out


def recover(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[int]:
    """Re-arm what :func:`recoverable` finds. Commits. Returns the ids re-armed.

    The re-arm is deliberately the same one
    :func:`app.routers.content.retry_publication` performs by hand, down to
    going through :func:`app.services.publishing_service.retry_hold`: a
    syndicated copy recovered here must wait behind its original for exactly the
    reason a hand-retried one must, and a copy that goes out before the original
    has a canonical URL to point at is a mistake with no undo.

    The ids come back rather than being dispatched here so the caller decides
    how — a beat task hands them to workers, a test asserts on them, and this
    module stays free of the broker.
    """
    publications = recoverable(db, limit=limit)
    if not publications:
        return []

    ready: list[int] = []
    for publication in publications:
        content = publication.content
        hold = publishing_service.retry_hold(content, publication)
        publication.status = (
            PublicationStatus.SCHEDULED if hold else PublicationStatus.PENDING
        )
        # Reset rather than kept. The attempts this row spent were spent on a
        # question — "is there a connection?" — that has a different answer now,
        # so counting them against the retry budget for the *publish* would
        # start a recovered row one attempt from terminal for no reason.
        publication.attempts = 0
        publication.error = None
        publication.scheduled_for = hold
        # The piece is no longer out of platforms to try. Without this it goes on
        # reading ``failed`` while a worker publishes it — the same arm
        # ``retry_publication`` needs, and for the same reason.
        publishing_service.sync_content_status(content)
        if hold is None:
            ready.append(publication.id)
        logger.info(
            "recovered publication %s to %s: %s is connected again",
            publication.id,
            publication.platform.value,
            publication.platform.value,
        )
    db.commit()
    return ready


__all__ = ["DEFAULT_LIMIT", "recover", "recoverable"]
