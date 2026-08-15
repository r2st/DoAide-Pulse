"""``publish_tasks``: the paths that only run when something has gone wrong.

The happy path through ``publish_one`` and the atomic claim are covered
elsewhere (``test_publishing_service.py``, ``test_publish_platform_dedupe.py``,
``test_stuck_publications.py``). What nothing exercised is what happens *after*
the claim goes wrong: a row deleted out from under a worker, a soft time limit
landing mid-publish, and ``cancel_publication`` — which had no test at all
despite being the only thing standing between a scheduled post and a user who
has changed their mind.
"""
from __future__ import annotations

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.tasks import publish_tasks


def _no_close(session):
    """The test session, wrapped so a task's ``db.close()`` does not end it."""

    class NoCloseProxy:
        closed = 0

        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            type(self).closed += 1

    return NoCloseProxy


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    proxy = _no_close(db)
    monkeypatch.setattr(publish_tasks, "SessionLocal", proxy)
    return proxy


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        title="Herald 1.0",
        slug="herald-1-0",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        body_markdown="It ships.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(
    db,
    content: Content,
    *,
    status: PublicationStatus = PublicationStatus.PENDING,
    platform: Platform = Platform.DEVTO,
) -> Publication:
    row = Publication(content_id=content.id, platform=platform, status=status)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# publish_one: the row is gone                                                 #
# --------------------------------------------------------------------------- #


def test_publishing_a_row_that_no_longer_exists_is_reported_not_raised(_task_session):
    """A piece deleted between dispatch and pickup.

    The claim matches nothing and ``db.get`` returns nothing. Raising would earn
    three Celery retries against a row that will never exist again.
    """
    result = publish_tasks.publish_one(4242)

    assert result == {"publication_id": 4242, "status": "missing"}
    assert _task_session.closed == 1


# --------------------------------------------------------------------------- #
# publish_one: the soft time limit                                             #
# --------------------------------------------------------------------------- #


def test_a_publish_that_runs_out_of_time_is_re_armed_rather_than_stranded(
    db, content, monkeypatch
):
    """Without this the row sits in ``publishing`` forever.

    The claim has already moved it out of every state the beat sweep looks at,
    so a worker killed at the hard limit would leave a publication no sweep can
    see and no user can retry. Recording the timeout puts it back on the retry
    queue — ``scheduled`` behind a backoff rather than ``pending``, which is the
    same terms every other retryable failure gets and the same thing
    ``execute``'s own timeout branch does. See
    ``publishing_service.record_timeout``.
    """
    publication = _publication(db, content)

    def _slow(session, row):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(publish_tasks.publishing_service, "execute", _slow)

    result = publish_tasks.publish_one(publication.id)

    assert result == {"publication_id": publication.id, "status": "timeout"}
    db.refresh(publication)
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.scheduled_for is not None, "due immediately is not a backoff"
    assert "timed out" in (publication.error or "")
    assert publication.attempts == 1, "the attempt was spent and must be counted"


def test_a_timeout_after_the_post_went_out_does_not_re_arm_it(db, content, monkeypatch):
    """The dangerous case: the platform accepted the post and *then* we ran out
    of time. Re-arming a published row would publish it a second time.
    """
    publication = _publication(db, content)

    def _publishes_then_times_out(session, row):
        row.status = PublicationStatus.PUBLISHED
        row.external_url = "https://dev.to/u/herald-1-0"
        session.commit()
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        publish_tasks.publishing_service, "execute", _publishes_then_times_out
    )

    result = publish_tasks.publish_one(publication.id)

    assert result == {"publication_id": publication.id, "status": "timeout"}
    db.refresh(publication)
    assert publication.status is PublicationStatus.PUBLISHED, (
        "a terminal row must not be dragged back into the retry queue"
    )
    assert publication.error is None


def test_a_timeout_whose_bookkeeping_also_fails_still_returns(db, content, monkeypatch):
    """The inner ``try`` exists because the session may be the reason we timed
    out. If recording the timeout raises too, the task must still return rather
    than turning a soft limit into an unhandled exception and three retries.
    """
    publication = _publication(db, content)

    def _slow(session, row):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(publish_tasks.publishing_service, "execute", _slow)

    calls = {"get": 0}
    real_get = type(db).get

    def _get(self, *args, **kwargs):
        calls["get"] += 1
        if calls["get"] > 1:  # the one inside the timeout handler
            raise RuntimeError("connection already gone")
        return real_get(db, *args, **kwargs)

    monkeypatch.setattr(type(db), "get", _get)

    result = publish_tasks.publish_one(publication.id)

    assert result == {"publication_id": publication.id, "status": "timeout"}


# --------------------------------------------------------------------------- #
# cancel_publication                                                           #
# --------------------------------------------------------------------------- #


def test_cancelling_a_scheduled_publication_stops_it(db, content):
    publication = _publication(db, content, status=PublicationStatus.SCHEDULED)
    publication.scheduled_for = utcnow()
    db.commit()

    assert publish_tasks.cancel_publication(publication.id) == {
        "publication_id": publication.id,
        "cancelled": True,
    }
    db.refresh(publication)
    assert publication.status is PublicationStatus.CANCELLED


def test_cancelling_a_publication_that_is_gone_is_not_an_error(_task_session):
    assert publish_tasks.cancel_publication(777) == {
        "publication_id": 777,
        "cancelled": False,
    }
    assert _task_session.closed == 1


@pytest.mark.parametrize(
    "status",
    [PublicationStatus.PUBLISHED, PublicationStatus.FAILED, PublicationStatus.CANCELLED],
)
def test_a_terminal_publication_cannot_be_cancelled(db, content, status):
    """Cancelling a published post would say "cancelled" about something that is
    live on the internet. The answer is no, and the status is left alone.
    """
    publication = _publication(db, content, status=status)

    assert publish_tasks.cancel_publication(publication.id)["cancelled"] is False
    db.refresh(publication)
    assert publication.status is status


# --------------------------------------------------------------------------- #
# publish_due: the broker-down fallback                                        #
# --------------------------------------------------------------------------- #


def test_the_sweep_publishes_inline_when_the_broker_is_unreachable(
    db, content, monkeypatch
):
    """A dropped publication is worse than a slow sweep.

    ``publish_one.delay`` raising means the queue is unreachable; the sweep runs
    the work on its own thread rather than returning as though it dispatched it.
    """
    publication = _publication(db, content)
    inline: list[int] = []

    def _broker_down(publication_id):
        raise ConnectionError("no broker")

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _broker_down)
    monkeypatch.setattr(
        publish_tasks.publishing_service,
        "execute",
        lambda session, row: inline.append(row.id),
    )

    result = publish_tasks.publish_due()

    assert result["dispatched"] == 1
    assert inline == [publication.id], "the due row must still have been published"


def test_the_sweep_reports_nothing_when_nothing_is_due(_task_session):
    assert publish_tasks.publish_due() == {
        "dispatched": 0,
        "failed": 0,
        "reclaimed": 0,
    }


def test_one_unpublishable_row_does_not_end_the_sweep(db, content, monkeypatch):
    """The isolation every other sweep states outright and this one lacked.

    The inline branch runs the publish on the sweep's own thread, so anything
    ``publish_one`` does not catch lands in this loop. Letting it escape would
    not lose one publication — it would drop every later row in the batch, and
    the next pass selects the same rows in the same order and dies in the same
    place, so one poison row is a standing outage of the whole pipeline rather
    than one failed post.
    """
    first = _publication(db, content, platform=Platform.DEVTO)
    second = _publication(db, content, platform=Platform.HASHNODE)
    third = _publication(db, content, platform=Platform.MASTODON)
    assert first.id < second.id < third.id

    published: list[int] = []

    def _broker_down(publication_id):
        raise ConnectionError("no broker")

    def _execute(session, row):
        if row.id == second.id:
            raise RuntimeError("adapter blew up outside execute's own try")
        published.append(row.id)

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _broker_down)
    monkeypatch.setattr(publish_tasks.publishing_service, "execute", _execute)

    result = publish_tasks.publish_due()

    # The row after the bad one still went out.
    assert third.id in published
    assert published == [first.id, third.id]
    assert result["failed"] == 1
    assert result["dispatched"] == 2


def test_the_sweep_does_not_count_a_row_it_dropped(db, content, monkeypatch):
    """``dispatched`` used to report the number *selected*.

    A pass that published nothing and reported "dispatched 3" is the kind of
    silent failure that makes a queue look healthy in the logs while nothing
    leaves it.
    """
    _publication(db, content)

    def _broker_down(publication_id):
        raise ConnectionError("no broker")

    def _always_blows_up(session, row):
        raise RuntimeError("nope")

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _broker_down)
    monkeypatch.setattr(
        publish_tasks.publishing_service, "execute", _always_blows_up
    )

    result = publish_tasks.publish_due()

    assert result["dispatched"] == 0
    assert result["failed"] == 1


def test_the_broker_outage_reaches_the_log_at_a_level_production_records(
    db, content, monkeypatch, caplog
):
    """This branch logged at DEBUG, which production does not record.

    So the one condition that turns a fan-out across the worker fleet into a
    serial inline publish of every due row left no trace an operator would see,
    and no hint of *why* the broker refused. The two other beat sweeps said
    nothing at all; all three request-path dispatchers already warned.
    """
    _publication(db, content)

    def _broker_down(publication_id):
        raise ConnectionError("redis is not listening")

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _broker_down)
    monkeypatch.setattr(
        publish_tasks.publishing_service, "execute", lambda session, row: None
    )

    with caplog.at_level("WARNING"):
        publish_tasks.publish_due()

    assert "redis is not listening" in caplog.text


def test_the_sweep_reports_one_broker_outage_not_one_per_publication(
    db, content, monkeypatch, caplog
):
    """One broker, one warning — however many rows were due."""
    for platform in (Platform.DEVTO, Platform.HASHNODE, Platform.MASTODON):
        _publication(db, content, platform=platform)

    def _broker_down(publication_id):
        raise ConnectionError("broker gone")

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _broker_down)
    monkeypatch.setattr(
        publish_tasks.publishing_service, "execute", lambda session, row: None
    )

    with caplog.at_level("WARNING"):
        result = publish_tasks.publish_due()

    assert result["dispatched"] == 3, "all three still went out"
    outage = [r for r in caplog.records if "broker unavailable" in r.message]
    assert len(outage) == 1


# --------------------------------------------------------------------------- #
# publish_due: the soft time limit                                             #
# --------------------------------------------------------------------------- #


def test_a_timeout_during_dispatch_is_not_reported_as_a_dead_broker(
    db, content, monkeypatch, caplog
):
    """The branch below reads any exception from ``.delay`` as an outage.

    ``SoftTimeLimitExceeded`` is an ordinary ``Exception``, so a sweep that ran
    out of time mid-dispatch was diagnosed as a broker failure — and answered by
    publishing that row *inline*, on a task with no time left, and then carrying
    on to the next one. The sweep then ran to the hard limit, which kills the
    worker outright, and ``acks_late`` handed the whole batch to the next worker
    to do again.

    The sibling sweeps all stop here. This one is the odd one out, and it is the
    one that runs most often.
    """
    for platform in (Platform.DEVTO, Platform.HASHNODE, Platform.MASTODON):
        _publication(db, content, platform=platform)

    inline: list[int] = []
    monkeypatch.setattr(
        publish_tasks.publish_one,
        "delay",
        lambda _id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    monkeypatch.setattr(
        publish_tasks.publishing_service,
        "execute",
        lambda session, row: inline.append(row.id),
    )

    with caplog.at_level("WARNING"):
        result = publish_tasks.publish_due()

    assert inline == [], "a task out of time must not start publishing inline"
    assert result["dispatched"] == 0
    assert "timed out" in caplog.text
    assert "broker unavailable" not in caplog.text


def test_a_timeout_while_publishing_inline_stops_the_sweep(
    db, content, monkeypatch, caplog
):
    """The likelier half: the broker is down and the sweep's own clock runs out.

    Inline publishing is serial and spends this task's budget, so it is where
    the soft limit actually lands — and it never reaches the sweep as an
    exception. ``publish_one`` catches its own ``SoftTimeLimitExceeded`` to
    record the timeout on the row, and Celery raises it once, so the outcome it
    returns is the only trace left.

    Read as an ordinary result, the sweep went on to the next row — a real
    publish attempt begun after the deadline — and the one after that, until the
    hard limit killed the worker and ``acks_late`` redelivered the batch.
    """
    first = _publication(db, content, platform=Platform.DEVTO)
    second = _publication(db, content, platform=Platform.HASHNODE)
    third = _publication(db, content, platform=Platform.MASTODON)
    assert first.id < second.id < third.id

    published: list[int] = []

    def _execute(session, row):
        # Raised where a real one lands: inside the adapter call, under
        # ``publish_one``'s own handler, which absorbs it and returns
        # ``{"status": "timeout"}``.
        if row.id == second.id:
            raise SoftTimeLimitExceeded()
        published.append(row.id)

    monkeypatch.setattr(
        publish_tasks.publish_one,
        "delay",
        lambda _id: (_ for _ in ()).throw(ConnectionError("no broker")),
    )
    monkeypatch.setattr(publish_tasks.publishing_service, "execute", _execute)

    with caplog.at_level("WARNING"):
        result = publish_tasks.publish_due()

    assert published == [first.id], "the sweep stopped rather than working on"
    assert third.id not in published
    assert result["dispatched"] == 2, "the timed-out row was still handed on"
    assert result["failed"] == 0, "a timeout is the sweep's fault, not the row's"
    assert "timed out" in caplog.text
