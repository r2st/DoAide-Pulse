"""The metrics sweep must not read content and project one publication at a time.

``collect_all_metrics`` walks every published publication on the install, and
``publishing_service.collect_metrics`` needs to know whose credentials to use.
It used to find out by reading ``publication.content.project.user_id``, and both
hops were lazy relationships, so the sweep paid two extra SELECTs per
publication — on the one task whose row count grows with everything the install
has ever published, and which runs on a timer whether anyone is looking or not.

Eager-loading them was only half a fix, and the visible half. The real sweep
commits a metric row per publication it records, and every commit expires the
session, so the second row and all after it re-read the publication, its whole
``Content`` — article body included — and its project, one at a time, exactly as
if nothing had been eagerly loaded. Every test here stubbed ``collect_metrics``,
and a stub does not commit, so the budget stayed green.

The owner now comes back as a column off the join and is handed to
``collect_metrics``, which is what survives a commit. ``test_the_sweep_survives_
its_own_commits`` runs the real poller, and is the one assertion in this file
that would have caught it.

Counted rather than timed: a query budget is the only assertion that stays true
on a fast machine.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishers
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import MetricsSnapshot
from app.tasks import metrics_tasks


def _no_close(session):
    """The test session, wrapped so the task's ``db.close()`` does not end it."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture
def four_published(db, project):
    """Four published pieces on a platform the poller actually polls.

    Four rather than one: the whole distinction under test is between a cost
    that is flat and one that scales, and a single row cannot tell them apart.
    """
    for index in range(4):
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
        db.add(
            Publication(
                content_id=content.id,
                platform=Platform.DEVTO,
                status=PublicationStatus.PUBLISHED,
                published_at=utcnow(),
                external_id=f"ext-{index}",
            )
        )
    db.commit()


def _selects_from(statements: list[str], table: str) -> list[str]:
    return [s for s in statements if s.startswith("SELECT") and f"FROM {table}" in s]


def test_the_sweep_does_not_read_content_once_per_publication(
    db, four_published, sql_log, monkeypatch
):
    """The N+1 itself: content and projects are not read per publication.

    ``collect_metrics`` is stubbed out because this test is about the cost of
    *finding* the publications, not about polling — leaving the real one in
    would add its credential lookup to the count and make the number under
    assertion depend on a second, unrelated query path.

    The stub takes the owner id the sweep now hands it, rather than walking
    ``publication.content.project`` to find it. That walk is the thing that was
    costing a query per row, and no longer happens at all — see
    ``test_the_sweep_survives_its_own_commits`` for why eager-loading it was
    only half a fix.
    """
    seen: list[int] = []

    def _record(session, publication, *, user_id=None, **kwargs):
        assert user_id, "the sweep must hand collect_metrics the owner"
        seen.append(publication.id)
        return None

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _record)
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    sql_log.clear()
    result = metrics_tasks.collect_all_metrics()

    assert result["polled"] == 4
    assert len(seen) == 4, "every published publication should have been visited"

    content_selects = _selects_from(sql_log, "content")
    project_selects = _selects_from(sql_log, "projects")
    assert len(content_selects) <= 1, (
        f"content was read {len(content_selects)} times for 4 publications — "
        "the relationship is loading lazily again:\n"
        + "\n".join(s[:140] for s in content_selects)
    )
    assert len(project_selects) <= 1, (
        f"projects was read {len(project_selects)} times for 4 publications:\n"
        + "\n".join(s[:140] for s in project_selects)
    )


def test_the_sweep_still_polls_every_published_publication(
    db, four_published, monkeypatch
):
    """Eager loading must change the cost and nothing else."""
    polled: list[str] = []

    def _record(session, publication, **kwargs):
        polled.append(publication.external_id)
        return None

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _record)
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    result = metrics_tasks.collect_all_metrics()

    assert sorted(polled) == ["ext-0", "ext-1", "ext-2", "ext-3"]
    assert result == {"polled": 4, "recorded": 0, "crossings": 0}


def test_a_publication_with_no_external_id_is_not_polled(db, project, monkeypatch):
    """The filter that keeps unpublished rows out of the sweep still applies.

    Asserted alongside the eager load because adding ``.options()`` to a query
    is exactly the kind of edit that can disturb its WHERE clause.
    """
    content = Content(
        project_id=project.id,
        title="No external id",
        slug="no-external-id",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow(),
            external_id=None,
        )
    )
    db.commit()

    monkeypatch.setattr(
        metrics_tasks.publishing_service,
        "collect_metrics",
        lambda *a, **k: pytest.fail("a row with no external id was polled"),
    )
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    assert metrics_tasks.collect_all_metrics() == {"polled": 0, "recorded": 0, "crossings": 0}


def test_the_sweep_survives_its_own_commits(
    db, user, project, four_published, sql_log, monkeypatch
):
    """The real poller, which commits a metric row per publication.

    Every other test in this file stubs ``collect_metrics``, and a stub does not
    commit. The real one does, once per publication it records, and a commit
    expires the session — so the eager load that made the *first* row cheap was
    discarded before the second, and the sweep went back to re-reading each
    publication, its whole ``Content`` and its project, one row at a time. Three
    SELECTs per publication and an article body among them, on the task whose
    row count grows with everything the install has ever published.

    Two assertions, because the count and the width are separate failures:

    * ``content`` and ``projects`` are read **at most once**, whatever the row
      count — the sweep joins them to get the owner and never touches the
      relationship per row.
    * No statement carries a body. Matched on the bare column name: a
      ``joinedload`` renders the entity it loads under an alias, so a qualified
      match would miss precisely the read this is about.

    The one permitted read of each is the engagement-threshold candidate query
    at the tail of the sweep (:func:`app.services.engagement_alerts.evaluate`),
    which joins ``content`` to ``projects`` for three scalar columns. It is one
    statement for the whole sweep rather than one per row, and it is narrow by
    construction: ``select(Content.id, ...)``, never ``select(Content)``. The
    body assertion below is what holds it to that — swap in the entity and this
    test says so, which is the whole reason the two assertions are separate.
    """
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda external_id, credentials: MetricsSnapshot(views=10),
    )
    monkeypatch.setattr(metrics_tasks, "SessionLocal", _no_close(db))

    sql_log.clear()
    assert metrics_tasks.collect_all_metrics() == {"polled": 4, "recorded": 4, "crossings": 0}

    content_selects = _selects_from(sql_log, "content")
    assert len(content_selects) <= 1, "\n".join(s[:200] for s in content_selects)
    assert _selects_from(sql_log, "projects") == []
    bodies = [s for s in sql_log if "body_markdown" in s]
    assert bodies == [], "\n".join(s[:200] for s in bodies)
