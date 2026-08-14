"""Reloading the rows a publish just wrote is one query, not one per platform.

Four endpoints commit a batch of publications and then hand them back to the
caller. The session expires everything on commit, so every one of them ended
with a ``for publication in ...: db.refresh(publication)`` — a SELECT per
platform, and on the publish path a *second* one per platform underneath it,
because the dispatch filter reads ``p.id`` and ``p.scheduled_for`` off rows that
are still expired.

Bounded by the platform count, so nothing here was ever slow. It is pinned
anyway because the bound is incidental: the loops grow with whatever the caller
passes, and the same shape written against a list that is not bounded is the
N+1 nobody notices until it is in production. Counted rather than timed, for the
reason :mod:`tests.test_calendar_query_budget` gives.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import inspect

from app.database import refresh_all
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus

#: Every platform with a finished adapter that the fixture connects. More than
#: one is the whole point: a single publication cannot tell "one query for the
#: batch" apart from "one query per row".
_PLATFORMS = (Platform.DEVTO, Platform.MASTODON, Platform.HASHNODE)


@pytest.fixture
def connected(db, user):
    for platform in _PLATFORMS:
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                encrypted_credentials="not-read-in-this-test",
                status=ConnectionStatus.CONNECTED,
            )
        )
    db.commit()


@pytest.fixture
def draft(db, project):
    content = Content(
        project_id=project.id,
        title="A piece for several platforms",
        slug="a-piece-for-several-platforms",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    return content


def _reloads(statements: list[str]) -> list[str]:
    """Statements that read publication rows back *by their own id*.

    Keyed on ``publications.id`` specifically, which is what both spellings of
    a reload look like — ``= ?`` for the per-row ``db.refresh`` this replaced,
    ``IN (?, ?, ?)`` for the batch. It deliberately excludes the ``content_id
    IN (...)`` load, which is ``Content.publications`` being populated by its
    ``lazy="selectin"`` relationship: that one is already a single query for
    the whole batch and is not what is being counted here.
    """
    return [
        s
        for s in statements
        if s.startswith("SELECT")
        and "FROM publications" in s
        and "WHERE publications.id" in s
    ]


def _later() -> str:
    return (utcnow() + timedelta(days=3)).isoformat()


def test_scheduling_a_cross_post_reloads_every_platform_in_one_query(
    client, auth, connected, draft, sql_log
):
    sql_log.clear()
    resp = client.post(
        f"/api/v1/content/{draft.id}/publish",
        json={"platforms": [p.value for p in _PLATFORMS], "scheduled_for": _later()},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == len(_PLATFORMS)

    reloads = _reloads(sql_log)
    assert len(reloads) == 1, (
        f"{len(reloads)} publication reads for {len(_PLATFORMS)} platforms — "
        "one per row is the N+1 this pins:\n"
        + "\n".join(s[:140] for s in reloads)
    )


def test_unscheduling_reloads_every_cancelled_row_in_one_query(
    client, auth, connected, draft, sql_log
):
    queued = client.post(
        f"/api/v1/content/{draft.id}/publish",
        json={"platforms": [p.value for p in _PLATFORMS], "scheduled_for": _later()},
        headers=auth,
    )
    assert queued.status_code == 200, queued.text

    sql_log.clear()
    resp = client.delete(f"/api/v1/content/{draft.id}/schedule", headers=auth)
    assert resp.status_code == 200, resp.text
    assert {row["status"] for row in resp.json()} == {"cancelled"}

    reloads = _reloads(sql_log)
    assert len(reloads) == 1, "\n".join(s[:140] for s in reloads)


def test_rescheduling_from_the_calendar_reloads_in_one_query(
    client, auth, connected, draft, sql_log
):
    queued = client.post(
        f"/api/v1/content/{draft.id}/publish",
        json={"platforms": [p.value for p in _PLATFORMS], "scheduled_for": _later()},
        headers=auth,
    )
    assert queued.status_code == 200, queued.text

    moved_to = (utcnow() + timedelta(days=9)).isoformat()
    sql_log.clear()
    resp = client.patch(
        f"/api/v1/calendar/content/{draft.id}",
        json={"scheduled_for": moved_to},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == len(_PLATFORMS)

    reloads = _reloads(sql_log)
    assert len(reloads) == 1, "\n".join(s[:140] for s in reloads)


def test_the_optimised_schedule_reloads_its_restaggered_rows_in_one_query(
    client, auth, connected, draft, sql_log
):
    """``optimize=true`` writes a different time per platform, then re-reads.

    The one call site of the four that commits a *second* time — it queues at a
    single moment and then spreads the platforms out — so it is the one where a
    refresh loop is easiest to leave behind.
    """
    sql_log.clear()
    resp = client.post(
        f"/api/v1/content/{draft.id}/schedule",
        json={"platforms": [p.value for p in _PLATFORMS], "optimize": True},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == len(_PLATFORMS)

    reloads = _reloads(sql_log)
    assert len(reloads) == 2, (
        "one reload for the queue, one for the restagger:\n"
        + "\n".join(s[:140] for s in reloads)
    )


# --------------------------------------------------------------------------- #
# The helper itself                                                            #
# --------------------------------------------------------------------------- #


def test_refresh_all_brings_back_values_another_session_wrote(db, connected, draft):
    """It has to actually refresh, not merely avoid the queries.

    A "batch refresh" that quietly did nothing would pass every budget above.
    """
    publication = Publication(
        content_id=draft.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(publication)
    db.commit()

    # Write past the ORM so the in-memory instance cannot know about it.
    db.execute(
        Publication.__table__.update()
        .where(Publication.__table__.c.id == publication.id)
        .values(external_id="written-behind-its-back")
    )

    refresh_all(db, [publication])
    assert publication.external_id == "written-behind-its-back"


def test_refresh_all_does_not_expand_the_batch_into_one_query_each(
    db, connected, draft, sql_log
):
    publications = [
        Publication(content_id=draft.id, platform=platform, status=PublicationStatus.PENDING)
        for platform in _PLATFORMS
    ]
    db.add_all(publications)
    db.commit()

    sql_log.clear()
    refresh_all(db, publications)
    assert len(_reloads(sql_log)) == 1, sql_log


def test_refresh_all_groups_a_mixed_batch_by_model(db, connected, draft, sql_log):
    """Two models, two queries — not one per row, and not a wrong-table lookup."""
    publication = Publication(
        content_id=draft.id, platform=Platform.DEVTO, status=PublicationStatus.PENDING
    )
    db.add(publication)
    db.commit()

    sql_log.clear()
    refresh_all(db, [publication, draft])

    selects = [s for s in sql_log if s.startswith("SELECT")]
    assert len(_reloads(selects)) == 1
    assert any("FROM content WHERE content.id IN" in s for s in selects)


def test_refresh_all_ignores_rows_that_were_never_persisted(db, connected, draft):
    """A transient instance has no row to reload — and must not raise.

    ``db.refresh`` raises for one of these. The batch form is called with
    whatever a route collected, so swallowing them is the difference between a
    helper that can be dropped into an existing loop and one that cannot.
    """
    transient = Publication(
        content_id=draft.id, platform=Platform.DEVTO, status=PublicationStatus.PENDING
    )
    assert inspect(transient).identity is None

    refresh_all(db, [transient])  # must not raise
    assert transient.external_id is None


def test_refresh_all_on_an_empty_batch_emits_nothing(db, sql_log):
    sql_log.clear()
    refresh_all(db, [])
    assert sql_log == []
