"""The terminal failure that nobody was told about.

A publication reaches ``failed`` down two paths, and only one of them said so.

``_fail`` is the path where a platform answered — a refusal, a timeout, a body
that would not parse — and on the attempt that spends the retry budget it fires
``publication.failed``. ``tests.test_publish_timeout_budget`` states why in one
line: a post that quietly stopped being retried is a post nobody knows is dead.

:func:`publishing_service.reclaim_stuck` is the other path, and it is the
quieter one. Nothing answered there at all — the worker was OOM-killed, or a
deploy restarted it, mid-publish — so there is no exception to carry and no
platform to blame. The row sits in ``publishing``, the sweep re-arms it, and on
the pass that takes ``attempts`` to the ceiling it goes terminal, takes the
piece to ``failed`` with it, and stops. That was the whole of it: a warning in a
worker log, and a subscription to ``publication.failed`` that never fired for
the failure most likely to need a human, since a publish that reliably kills its
worker is not something the account can fix by waiting.

The same path also left ``scheduled_for`` where it was, which ``_fail`` is
careful to clear — see the comment there, and the ordering
``routers.content.publication_queue`` is built on.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import Webhook, WebhookEvent
from app.services import publishing_service, webhooks


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Pulse 1.0 is out.",
        meta_description="Pulse 1.0 is out.",
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _abandoned(db, content, *, attempts: int, scheduled_for=None) -> Publication:
    """A row claimed long enough ago that no worker is still behind it."""
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHING,
        attempts=attempts,
        scheduled_for=scheduled_for,
    )
    db.add(row)
    db.commit()
    # After the insert: ``updated_at`` has an ``onupdate``, so a value set on the
    # way in is overwritten by the flush that stores it.
    row.updated_at = utcnow() - timedelta(
        seconds=settings.publish_stuck_after_seconds + 60
    )
    db.commit()
    return row


@pytest.fixture
def fired(monkeypatch) -> list[WebhookEvent]:
    events: list[WebhookEvent] = []
    monkeypatch.setattr(
        publishing_service.webhooks,
        "emit",
        lambda db, *, user_id, event, data: events.append(event),
    )
    return events


def test_the_last_attempt_dying_with_its_worker_still_tells_the_user(
    db, content, fired
):
    publication = _abandoned(db, content, attempts=settings.publish_max_retries)

    publishing_service.reclaim_stuck(db)

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    assert fired == [WebhookEvent.PUBLICATION_FAILED]


def test_a_row_that_still_has_retries_left_notifies_nobody(db, content, fired):
    """Mid-retry is not news — the same rule ``_notify_failed`` states."""
    publication = _abandoned(db, content, attempts=1)

    publishing_service.reclaim_stuck(db)

    db.refresh(publication)
    assert publication.status == PublicationStatus.PENDING
    assert fired == []


def test_the_notice_describes_a_row_already_committed_as_failed(db, content, monkeypatch):
    """Fired after the commit, so an endpoint reading the API back agrees with it.

    The ordering ``_fail`` and ``_notify_published`` both keep. A notice sent
    from inside the loop would describe a row still ``publishing`` to anything
    that went and looked.
    """
    publication = _abandoned(db, content, attempts=settings.publish_max_retries)
    seen: list[PublicationStatus] = []

    def _emit(db_, *, user_id, event, data):
        seen.append(db_.get(Publication, publication.id).status)

    monkeypatch.setattr(publishing_service.webhooks, "emit", _emit)

    publishing_service.reclaim_stuck(db)

    assert seen == [PublicationStatus.FAILED]


def test_a_row_that_dies_terminally_stops_claiming_a_slot_on_the_calendar(db, content):
    """The stale ``scheduled_for`` a deferred retry left on the row.

    ``_fail`` parks a non-terminal failure as ``scheduled`` with a time on it,
    and ``execute``'s claim moves the row to ``publishing`` without clearing
    that time. So the row arriving in this sweep is carrying the schedule of a
    retry that has already happened — and ``routers.calendar`` filters on the
    column with no reference to status, so a publication that is finished and
    failed sat on the calendar as a post still to come.
    """
    publication = _abandoned(
        db,
        content,
        attempts=settings.publish_max_retries,
        scheduled_for=utcnow() - timedelta(minutes=5),
    )

    publishing_service.reclaim_stuck(db)

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    assert publication.scheduled_for is None


def test_notifying_a_whole_dead_batch_does_not_reread_it_row_by_row(
    db, project, user, sql_log, monkeypatch
):
    """The budget ``tests.test_reclaim_stuck_budget`` pins, with a subscriber.

    That module measures the sweep with nothing subscribed, so ``emit`` returns
    before its commit and the rows stay loaded whatever the notices do. A user
    who actually has a webhook is the case this ordering was written for:
    ``emit`` commits, which expires every row the *remaining* notices were going
    to be read from, so a batch built out of publications rather than out of
    finished bodies pays a re-fetch of the piece, its project and its sibling
    rows for each one after the first.
    """
    monkeypatch.setattr(webhooks, "dispatch", lambda ids: None)
    db.add(
        Webhook(
            user_id=user.id,
            url="https://example.com/hook",
            events=[WebhookEvent.PUBLICATION_FAILED.value],
            encrypted_secret=webhooks.store_secret("s"),
        )
    )
    db.commit()

    def _burn(count: int, offset: int) -> int:
        for i in range(offset, offset + count):
            piece = Content(
                project_id=project.id,
                content_type=ContentType.ANNOUNCEMENT,
                status=ContentStatus.APPROVED,
                title=f"Post {i}",
                slug=f"post-{i}",
                body_markdown="Body words here.",
            )
            db.add(piece)
            db.flush()
            db.add(
                Publication(
                    content_id=piece.id,
                    platform=Platform.DEVTO,
                    status=PublicationStatus.PUBLISHING,
                    attempts=settings.publish_max_retries - 1,
                )
            )
        db.commit()
        db.query(Publication).filter(
            Publication.status == PublicationStatus.PUBLISHING
        ).update(
            {
                Publication.updated_at: utcnow()
                - timedelta(seconds=settings.publish_stuck_after_seconds + 60)
            }
        )
        db.commit()
        # Otherwise the sweep reads what this function left in the identity map
        # instead of doing the loads under test.
        db.expire_all()
        sql_log.clear()
        publishing_service.reclaim_stuck(db)
        # Reads of the rows themselves. ``webhooks`` is deliberately not counted:
        # one lookup and one delivery insert per notice is what emitting N
        # notices *is*, and it does not grow with anything but N.
        return len(
            [
                s
                for s in sql_log
                if s.startswith("SELECT")
                and any(
                    f"FROM {table}" in s
                    for table in ("content", "publications", "projects")
                )
            ]
        )

    few = _burn(2, 0)
    many = _burn(12, 100)

    assert few == many, (
        f"{few} row SELECTs for 2 burned rows, {many} for 12 — the notices are "
        "being read out of rows the commit expired"
    )


def test_a_re_armed_row_keeps_whatever_time_it_was_carrying(db, content):
    """Only the terminal branch clears it.

    A row going back to ``pending`` is due immediately by status, and rewriting
    its history is not this sweep's business.
    """
    when = utcnow() - timedelta(minutes=5)
    publication = _abandoned(db, content, attempts=1, scheduled_for=when)

    publishing_service.reclaim_stuck(db)

    db.refresh(publication)
    assert publication.status == PublicationStatus.PENDING
    assert publication.scheduled_for is not None
