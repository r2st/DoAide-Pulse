"""The query budget of the sweep that runs when a worker has already died.

``reclaim_stuck`` re-arms every publication a dead worker left in ``publishing``,
and the row that has spent its retries is failed outright — which means calling
``_sync_content_status``, which reads ``publication.content`` and then that
content's own publications to decide whether the piece as a whole is finished.

Both hops were lazy. ``Publication.content`` is a plain relationship, so each
burned row fetched its content on its own; ``Content.publications`` is
``lazy="selectin"``, so each of *those* contents then fetched its siblings on its
own. Two SELECTs per burned row, in the one sweep whose busy days are the days
something has already gone wrong: a worker OOM-killed mid-batch, or a deploy
restarting the service, leaves every row it had claimed for this pass to clean up
at once.

The assertions are that neither count grows between a small batch and a large
one. That is the property — an absolute number would need editing every time an
unrelated query is added beside it.

Every publication gets its **own** content, for the reason ``test_n_plus_one``
gives: shared parents are served from the identity map, and an N+1 measured
through one is not measured at all.
"""
from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service


def _seed_stuck(db, project_id: int, count: int, *, offset: int = 0) -> None:
    """*count* rows abandoned in ``publishing`` with their retries already spent.

    ``attempts`` starts one below the ceiling so the increment ``reclaim_stuck``
    applies takes each row terminal — the branch that calls
    ``_sync_content_status``, and the only one that touches the relationships
    this module is about.
    """
    stale = utcnow() - timedelta(seconds=settings.publish_stuck_after_seconds + 60)
    for i in range(offset, offset + count):
        content = Content(
            project_id=project_id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.APPROVED,
            title=f"Post {i}",
            slug=f"post-{i}",
            body_markdown="Body words here.",
        )
        db.add(content)
        db.flush()
        db.add(
            Publication(
                content_id=content.id,
                platform=Platform.DEVTO,
                status=PublicationStatus.PUBLISHING,
                attempts=settings.publish_max_retries - 1,
            )
        )
    db.commit()
    # ``updated_at`` carries an ``onupdate``, so the backdating that puts these
    # rows past the stuck cutoff has to happen after they are stored.
    db.query(Publication).update({Publication.updated_at: stale})
    db.commit()
    # Otherwise the sweep reads the objects this function just left behind in
    # the identity map instead of doing the loads under test.
    db.expire_all()


def _selects(sql_log: list[str], table: str) -> list[str]:
    return [s for s in sql_log if s.startswith("SELECT") and f"FROM {table}" in s]


def test_reclaiming_more_rows_does_not_fetch_their_content_one_at_a_time(
    db, project, sql_log
):
    _seed_stuck(db, project.id, 2)
    sql_log.clear()
    publishing_service.reclaim_stuck(db)
    few = len(_selects(sql_log, "content"))

    _seed_stuck(db, project.id, 12, offset=100)
    sql_log.clear()
    publishing_service.reclaim_stuck(db)
    many = len(_selects(sql_log, "content"))

    assert few == many, (
        f"{few} content SELECTs for 2 stuck rows, {many} for 12 — "
        "the content of each burned row is being fetched on its own"
    )


def test_reclaiming_more_rows_does_not_fetch_sibling_publications_per_row(
    db, project, sql_log
):
    # The transitive half: ``Content.publications`` is ``lazy="selectin"``, so a
    # content loaded one at a time drags its siblings back one batch at a time.
    _seed_stuck(db, project.id, 2)
    sql_log.clear()
    publishing_service.reclaim_stuck(db)
    few = len(_selects(sql_log, "publications"))

    _seed_stuck(db, project.id, 12, offset=100)
    sql_log.clear()
    publishing_service.reclaim_stuck(db)
    many = len(_selects(sql_log, "publications"))

    assert few == many, (
        f"{few} publications SELECTs for 2 stuck rows, {many} for 12"
    )


def test_the_sweep_still_burns_the_rows_it_reclaims(db, project):
    # The budget is only worth having if the work still happens: a load that is
    # eager but wrong would pass both tests above and fail every user.
    _seed_stuck(db, project.id, 3)

    assert publishing_service.reclaim_stuck(db) == 3

    rows = db.query(Publication).all()
    assert [p.status for p in rows] == [PublicationStatus.FAILED] * 3
    assert all(p.attempts == settings.publish_max_retries for p in rows)
    assert all("stopped before it finished" in (p.error or "") for p in rows)


def test_the_pieces_behind_them_are_marked_failed(db, project):
    # ``_sync_content_status`` is what the eager load exists to feed, and its
    # whole output is this: every publication of the piece is terminal and none
    # succeeded, so the piece itself failed.
    _seed_stuck(db, project.id, 3)

    publishing_service.reclaim_stuck(db)

    assert [c.status for c in db.query(Content).all()] == [ContentStatus.FAILED] * 3
