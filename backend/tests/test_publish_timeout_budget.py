"""A publish that times out somewhere ``execute`` cannot see must still cost a try.

Celery delivers a soft time limit by raising inside whatever the task is doing.
Nearly always that is the adapter's HTTP call, which sits inside
:func:`publishing_service.execute`'s ``try`` — :mod:`tests.test_publish_soft_timeout`
covers that path, and it puts the timeout on the retry budget like any other
transient failure.

This file is about the rest of the surface. ``publish_one`` does work outside
that ``try``: the atomic claim, and then ``execute``'s own prologue, which walks
from the publication to its content, its project and its owner before the first
``try`` opens. A timeout landing anywhere in there goes to ``publish_one``'s
handler instead, and that handler used to set the row back to ``pending`` and
stop.

Which made the retry free, in the exact shape :func:`publishing_service.reclaim_stuck`
documents and guards against for the worker that is *killed* rather than timed
out. ``execute`` increments ``attempts`` and flushes without committing, so a
timeout in the prologue rolls the increment back; a timeout before ``execute``
never made it. Either way the row came back at the count it went in with, so:

    claim → time out → roll back → re-arm at the same number → claim …

forever, with "Task timed out" on the row the whole way and nothing ever
reaching the ``publication.failed`` webhook. A publication whose content row is
pathologically slow to load — the case this handler exists for — could not fail,
and so could never be looked at by a human.

Two things are asserted here: that the budget is spent, and that spending it
ends the row rather than cycling it.
"""
from __future__ import annotations

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import WebhookEvent
from app.tasks import publish_tasks


@pytest.fixture(autouse=True)
def _task_session(monkeypatch, task_session):
    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="pulse-1-0",
        body_markdown="It ships.",
        status=ContentStatus.APPROVED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def publication(db, content) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def times_out_before_execute(monkeypatch):
    """A timeout raised where ``execute``'s own handler cannot catch it."""

    def _slow(session, row):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(publish_tasks.publishing_service, "execute", _slow)


def _make_due(db, publication) -> None:
    """What the beat sweep does when the backoff has elapsed."""
    publication.status = PublicationStatus.PENDING
    publication.scheduled_for = None
    db.commit()


def test_one_timeout_spends_one_attempt(db, publication, times_out_before_execute):
    """The count is the whole point: an uncounted attempt is an unbounded loop."""
    publish_tasks.publish_one(publication.id)

    db.refresh(publication)
    assert publication.attempts == 1


def test_a_publication_that_always_times_out_eventually_fails(
    db, publication, times_out_before_execute
):
    """The loop this file is named for, run until it either ends or proves it
    does not."""
    for _ in range(settings.publish_max_retries + 5):
        publish_tasks.publish_one(publication.id)
        db.refresh(publication)
        if publication.is_terminal:
            break
        _make_due(db, publication)

    db.refresh(publication)
    assert publication.status is PublicationStatus.FAILED
    assert publication.attempts == settings.publish_max_retries
    assert "timed out" in (publication.error or "")


def test_the_retries_are_spread_out_rather_than_spent_at_the_sweep_cadence(
    db, publication, times_out_before_execute
):
    """``pending`` is due immediately, which is not a backoff.

    Three attempts inside ten minutes answers a blip and nothing else — the
    reasoning ``_fail`` gives at length, and a timeout has no more claim on an
    exception from it than any other transient failure.
    """
    publish_tasks.publish_one(publication.id)
    db.refresh(publication)
    first = publication.scheduled_for

    _make_due(db, publication)
    publish_tasks.publish_one(publication.id)
    db.refresh(publication)

    assert first is not None and publication.scheduled_for is not None
    assert publication.status is PublicationStatus.SCHEDULED


def test_the_terminal_timeout_tells_the_user_about_it(
    db, user, publication, times_out_before_execute, monkeypatch
):
    """A post that quietly stopped being retried is a post nobody knows is dead.

    ``_fail`` fires ``publication.failed`` on the terminal attempt, and routing
    the timeout through it is what puts this path on that notice at all.
    """
    fired: list[WebhookEvent] = []
    monkeypatch.setattr(
        publish_tasks.publishing_service.webhooks,
        "emit",
        lambda db, *, user_id, event, data: fired.append(event),
    )

    for _ in range(settings.publish_max_retries):
        publish_tasks.publish_one(publication.id)
        db.refresh(publication)
        if publication.is_terminal:
            break
        _make_due(db, publication)

    assert fired == [WebhookEvent.PUBLICATION_FAILED]


def test_the_piece_follows_its_last_platform_to_failed(
    db, content, publication, times_out_before_execute
):
    """One platform, and it is finished: the piece is finished too.

    ``_fail`` syncs the content status on a terminal failure. The old handler
    never reached a terminal failure at all, so a piece whose only publication
    was looping showed as ``approved`` indefinitely.
    """
    for _ in range(settings.publish_max_retries):
        publish_tasks.publish_one(publication.id)
        db.refresh(publication)
        if publication.is_terminal:
            break
        _make_due(db, publication)

    db.refresh(content)
    assert content.status is ContentStatus.FAILED


def test_a_timeout_does_not_double_count_the_attempt_execute_had_started(
    db, publication, monkeypatch
):
    """``execute`` flushes ``attempts += 1`` before it calls the adapter.

    A timeout in the prologue rolls that flush back with the transaction, so the
    handler counting the attempt is the *only* count — but if the rollback were
    dropped, the flushed increment would still be in the session and the commit
    inside ``_fail`` would persist both. One spent attempt, one increment.
    """

    def _flushes_then_times_out(session, row):
        row.status = PublicationStatus.PUBLISHING
        row.attempts += 1
        session.flush()
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        publish_tasks.publishing_service, "execute", _flushes_then_times_out
    )

    publish_tasks.publish_one(publication.id)

    db.refresh(publication)
    assert publication.attempts == 1
