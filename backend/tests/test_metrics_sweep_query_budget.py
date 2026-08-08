"""The metrics sweep must not read content and project one publication at a time.

``collect_all_metrics`` walks every published publication on the install, and
``publishing_service.collect_metrics`` reads
``publication.content.project.user_id`` to work out whose credentials to use.
Both hops were lazy relationships, so the sweep paid two extra SELECTs per
publication — on the one task whose row count grows with everything the install
has ever published, and which runs on a timer whether anyone is looking or not.

Counted rather than timed: a query budget is the only assertion that stays true
on a fast machine.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
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
    """The N+1 itself: one SELECT for content and one for projects, not four.

    ``collect_metrics`` is stubbed out because this test is about the cost of
    *finding* the publications, not about polling — leaving the real one in
    would add its credential lookup to the count and make the number under
    assertion depend on a second, unrelated query path.
    """
    seen: list[int] = []

    def _record(session, publication, **kwargs):
        # Touch exactly what the real one touches, so a lazy load would show up.
        assert publication.content.project.user_id
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
    assert result == {"polled": 4, "recorded": 0}


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

    assert metrics_tasks.collect_all_metrics() == {"polled": 0, "recorded": 0}
