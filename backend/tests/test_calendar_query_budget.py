"""The calendar must read the metric series once, not twice.

``GET /calendar`` builds :func:`app.services.velocity.curves` up front so that
the per-platform ``learned_cadence.learn`` calls in its suggestion loop share
one pass over ``content_metrics`` — the comment above that loop says as much.
It then called ``learned_cadence.describe_all``, which had no way to be handed
the curves and so built its own set: the whole series, read a second time, on
every calendar load. The cost scales with everything the user has ever
published, and the calendar is a page people leave open.

Counted rather than timed: a query budget is the only assertion that stays true
on a fast machine.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus


@pytest.fixture
def published_with_metrics(db, project, user):
    """Two connected platforms, each with a published piece and a snapshot.

    Two rather than one: ``describe_all`` iterates platforms, so a single
    platform would not distinguish "one pass shared across the loop" from "one
    pass per platform".
    """
    for platform in (Platform.DEVTO, Platform.MASTODON):
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                encrypted_credentials="not-read-in-this-test",
                status=ConnectionStatus.CONNECTED,
            )
        )
    db.commit()

    for index, platform in enumerate((Platform.DEVTO, Platform.MASTODON)):
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
            platform=platform,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow() - timedelta(days=5),
            external_id=f"ext-{index}",
        )
        db.add(publication)
        db.commit()
        db.add(
            ContentMetric(
                publication_id=publication.id,
                views=100 + index,
                captured_at=utcnow() - timedelta(days=4),
            )
        )
    db.commit()


def _metric_scans(statements: list[str]) -> list[str]:
    return [
        s
        for s in statements
        if "FROM content_metrics" in s and "max(content_metrics.id)" not in s
    ]


def test_the_calendar_reads_the_metric_series_once(
    client, auth, published_with_metrics, sql_log
):
    sql_log.clear()
    resp = client.get("/api/v1/calendar", headers=auth)
    assert resp.status_code == 200, resp.text

    scans = _metric_scans(sql_log)
    assert len(scans) == 1, (
        f"expected one pass over content_metrics, got {len(scans)}:\n"
        + "\n".join(s[:120] for s in scans)
    )


def test_the_calendar_still_answers_with_the_cadence_it_shares_curves_with(
    client, auth, published_with_metrics
):
    """Sharing the curves must not change the answer, only the cost."""
    resp = client.get("/api/v1/calendar", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    platforms = {row["platform"] for row in body["cadence"]}
    assert platforms == {"devto", "mastodon"}
    for row in body["cadence"]:
        # Whichever way it was answered, the row has to say which — a suggested
        # time the user cannot interrogate is one they are right to ignore.
        assert row["source"] in {"learned", "table"}
        assert row["best_time_utc"]


def test_describe_all_still_builds_its_own_curves_when_not_given_any(db, user):
    """The parameter is an optimisation, not a new requirement on callers.

    ``cadence_guide`` and the digest still call this with two arguments, and
    passing ``known=None`` has to keep meaning "work it out yourself" rather
    than "there is no history".
    """
    from app.services import learned_cadence

    rows = learned_cadence.describe_all(db, user.id, [Platform.DEVTO])
    assert len(rows) == 1
    assert rows[0]["platform"] == "devto"


def test_describe_all_takes_any_sequence_of_either_spelling(db, user):
    """Same widening as ``scheduling.optimal_slots``, for the same reason.

    Both call sites in ``routers/calendar`` hold one concrete element type —
    ``list[Platform]`` from the connected platforms, ``list[str]`` from the
    query parameter — and ``list`` is invariant, so neither satisfied a
    ``list[Platform | str]`` parameter. Nothing here mutates the argument.
    """
    from app.services import learned_cadence

    from_tuple = learned_cadence.describe_all(db, user.id, (Platform.DEVTO,), known=[])
    from_strings = learned_cadence.describe_all(db, user.id, ["devto"], known=[])

    assert [row["platform"] for row in from_tuple] == ["devto"]
    assert from_strings == from_tuple


def test_an_empty_curve_list_is_not_mistaken_for_no_curves_at_all(db, user):
    """``known=[]`` means "I looked and there were none", not "I did not look".

    The distinction matters because the calendar passes ``[]`` whenever learned
    cadence is switched off, and re-deriving the curves there would undo the
    saving on exactly the installs that opted out of paying for them.
    """
    from app.services import learned_cadence

    rows = learned_cadence.describe_all(db, user.id, [Platform.DEVTO], known=[])
    assert rows[0]["source"] == "table"
    assert rows[0]["sample"] == 0
