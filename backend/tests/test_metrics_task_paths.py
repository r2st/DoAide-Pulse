"""``metrics_tasks``: the single-post refresh, and the sweep's two exits.

``test_metrics_sweep_query_budget.py`` covers what the sweep costs and
``test_metrics_fetch.py`` covers the polling underneath it. Untouched were
``collect_one`` — the whole of it, which is what the UI's "refresh" button calls
— and the two ways ``collect_all_metrics`` stops early: no platform worth
polling, and running out of time part-way through.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.tasks import metrics_tasks


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
    monkeypatch.setattr(metrics_tasks, "SessionLocal", proxy)
    return proxy


def _published(db, project, index: int = 0) -> Publication:
    content = Content(
        project_id=project.id,
        title=f"Piece {index}",
        slug=f"piece-{index}",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow(),
        external_id=f"ext-{index}",
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return publication


# --------------------------------------------------------------------------- #
# collect_all_metrics: nothing worth polling                                   #
# --------------------------------------------------------------------------- #


def test_no_platform_reports_metrics_so_the_sweep_never_opens_a_session(
    db, project, monkeypatch, _task_session
):
    """The guard is there to keep beat from walking every published row on the
    install to discover there is nothing to ask. It has to come *before* the
    session, or the cheap answer still costs a connection every five minutes.
    """
    _published(db, project)
    monkeypatch.setattr(
        metrics_tasks.publishers,
        "all_adapters",
        lambda: [
            SimpleNamespace(
                platform=Platform.DEVTO, implemented=True, supports_metrics=False
            ),
            # Implemented is the other half of the filter: an adapter that would
            # report metrics but cannot publish at all is not polled either.
            SimpleNamespace(
                platform=Platform.MEDIUM, implemented=False, supports_metrics=True
            ),
        ],
    )

    assert metrics_tasks.collect_all_metrics() == {"polled": 0, "recorded": 0}
    assert _task_session.closed == 0, "no session should have been opened at all"


def test_a_platform_that_reports_metrics_is_polled(db, project, monkeypatch):
    """The other side of the same filter — otherwise the test above would pass
    with a sweep that is simply broken.
    """
    publication = _published(db, project)
    polled: list[int] = []
    monkeypatch.setattr(
        metrics_tasks.publishing_service,
        "collect_metrics",
        lambda session, row, **kwargs: polled.append(row.id),
    )

    result = metrics_tasks.collect_all_metrics()

    assert polled == [publication.id]
    assert result["polled"] == 1


# --------------------------------------------------------------------------- #
# collect_all_metrics: the soft time limit                                     #
# --------------------------------------------------------------------------- #


def test_a_sweep_that_runs_out_of_time_returns_what_it_managed(
    db, project, monkeypatch
):
    """The sweep walks every published post on the install, so it is the task
    most likely to meet its limit. Catching it means the snapshots already
    written are reported and committed rather than the worker being killed.
    """
    for index in range(4):
        _published(db, project, index)
    seen: list[int] = []

    def _collect(session, publication, **kwargs):
        seen.append(publication.id)
        if len(seen) == 3:
            raise SoftTimeLimitExceeded()
        return ContentMetric(publication_id=publication.id, views=1)

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _collect)

    result = metrics_tasks.collect_all_metrics()

    assert len(seen) == 3, "the sweep must stop at the timeout, not carry on"
    assert result == {"polled": 4, "recorded": 2}


def test_one_broken_platform_does_not_stop_the_sweep(db, project, monkeypatch):
    """A malformed response from one platform must not cost the others their
    snapshot — they are unrelated APIs that happen to share a loop.
    """
    for index in range(3):
        _published(db, project, index)
    seen: list[int] = []

    def _collect(session, publication, **kwargs):
        seen.append(publication.id)
        if len(seen) == 2:
            raise ValueError("that was not JSON")
        return ContentMetric(publication_id=publication.id, views=1)

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _collect)

    result = metrics_tasks.collect_all_metrics()

    assert len(seen) == 3
    assert result == {"polled": 3, "recorded": 2}


# --------------------------------------------------------------------------- #
# collect_one — the refresh button                                             #
# --------------------------------------------------------------------------- #


def test_refreshing_one_publication_records_a_snapshot(db, project, monkeypatch):
    publication = _published(db, project)
    monkeypatch.setattr(
        metrics_tasks.publishing_service,
        "collect_metrics",
        lambda session, row: ContentMetric(publication_id=row.id, views=12),
    )

    assert metrics_tasks.collect_one(publication.id) == {
        "publication_id": publication.id,
        "recorded": True,
    }


def test_refreshing_a_publication_the_platform_has_nothing_for_records_nothing(
    db, project, monkeypatch
):
    """``collect_metrics`` returns ``None`` for an unsupported platform, a
    missing credential or a rate limit. All three mean "no snapshot", and the
    task has to say so rather than claim one was written.
    """
    publication = _published(db, project)
    monkeypatch.setattr(
        metrics_tasks.publishing_service, "collect_metrics", lambda session, row: None
    )

    assert metrics_tasks.collect_one(publication.id) == {
        "publication_id": publication.id,
        "recorded": False,
    }


def test_refreshing_a_publication_that_is_gone_is_not_an_error(_task_session):
    """The refresh button on a page that was open while the piece was deleted."""
    assert metrics_tasks.collect_one(31337) == {
        "publication_id": 31337,
        "recorded": False,
    }
    assert _task_session.closed == 1


def test_the_single_refresh_does_not_share_a_rate_limit_memo(db, project, monkeypatch):
    """``collect_one`` passes no ``rate_limited`` set, on purpose.

    The sweep's memo exists so one 429 stands the poller down for the rest of
    *that pass*. A person pressing refresh is a new decision, and inheriting a
    sweep's memo would make the button silently do nothing.
    """
    publication = _published(db, project)
    kwargs_seen: list[dict] = []

    def _collect(session, row, **kwargs):
        kwargs_seen.append(kwargs)
        return None

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _collect)

    metrics_tasks.collect_one(publication.id)

    assert kwargs_seen == [{}]
