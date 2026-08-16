"""``publications.duration_ms`` — how long the platform took, on the row.

The success rate in ``/api/v1/metrics`` cannot show a destination going bad,
because it is 1.0 right up until the moment it is not. Latency moves first: a
platform starts taking eight seconds, then twenty, then the worker hits its soft
time limit and the row is charged a retry for it. None of that was recorded
anywhere, so the first evidence of a slow platform was a piece in ``failed``.

What is pinned here is mostly *which attempts get a number and which do not*.
The measurement itself is a subtraction; the interesting part is that a failed
call is timed (it is the one that matters), that a call which never happened is
NULL rather than zero, and that the mean in the metrics payload counts only the
rows that have one.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, PublicationStatus
from app.services import ops_metrics, publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers import breaker
from app.services.publishers.base import PublishError, PublishResult
from app.services.publishers.devto import DevToAdapter


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@r2st",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def succeeds(monkeypatch):
    def publish(self, request, credentials):
        return PublishResult(external_id="1", external_url="https://dev.to/p/1")

    monkeypatch.setattr(DevToAdapter, "publish", publish)


@pytest.fixture
def fails(monkeypatch):
    def publish(self, request, credentials):
        raise PublishError("devto is down")

    monkeypatch.setattr(DevToAdapter, "publish", publish)


# --------------------------------------------------------------------------- #
# What gets timed                                                              #
# --------------------------------------------------------------------------- #


def test_a_published_row_carries_how_long_the_platform_took(
    db, content, connected, succeeds
):
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.duration_ms is not None
    assert publication.duration_ms >= 0


def test_a_failed_attempt_is_timed_too(db, content, connected, fails):
    """The half that matters most.

    A platform taking forty seconds to refuse a post is what puts a worker on
    its soft time limit, and timing only the successes would report that
    platform's latency as its good days.
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    publishing_service.execute(db, publication)

    assert publication.status != PublicationStatus.PUBLISHED
    assert publication.duration_ms is not None


def test_a_queued_row_has_no_duration_yet(db, content, connected):
    """NULL is "nothing has been sent", which is not "it took no time"."""
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    assert publication.duration_ms is None


def test_a_row_the_breaker_parked_is_left_untimed(db, content, connected, succeeds):
    """Nothing reached the platform, so there is nothing to say about it.

    Recording a zero here would be the worst case for the mean: the rows a
    tripped breaker parks are the ones queued *behind an outage*, so a whole
    bad afternoon would push the platform's average latency towards zero.
    """
    for _ in range(20):
        breaker.record_failure(Platform.DEVTO, content.project.user_id)
    assert breaker.is_open(Platform.DEVTO, content.project.user_id)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.duration_ms is None


def test_the_number_belongs_to_the_last_attempt(db, content, connected, monkeypatch):
    """One column, overwritten per attempt — so it tracks the platform *now*.

    A retried row keeping its first attempt's timing would be reporting how
    slow the platform was during the outage it has since recovered from.
    """
    durations = iter([PublishError("down"), PublishResult("1", "https://dev.to/p/1")])

    def publish(self, request, credentials):
        outcome = next(durations)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)
    first = publication.duration_ms

    publication.status = PublicationStatus.PENDING
    publication.scheduled_for = None
    db.commit()
    publishing_service.execute(db, publication)

    assert first is not None
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.duration_ms is not None


# --------------------------------------------------------------------------- #
# What the metrics payload does with it                                        #
# --------------------------------------------------------------------------- #


def test_the_metrics_endpoint_reports_platform_latency(
    db, client, auth, user, content, connected, succeeds
):
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    body = client.get("/api/v1/metrics", headers=auth).json()["publishing"]

    assert body["timed"] == 1
    assert body["avg_duration_ms"] is not None
    devto = next(p for p in body["by_platform"] if p["platform"] == "devto")
    assert devto["timed"] == 1
    assert devto["avg_duration_ms"] == publication.duration_ms


def test_an_install_that_has_timed_nothing_reports_null_rather_than_zero(
    db, client, auth, content, connected
):
    """Zero milliseconds is the one value that reads as the fastest possible."""
    publishing_service.queue(db, content, ["devto"])
    db.commit()

    body = client.get("/api/v1/metrics", headers=auth).json()["publishing"]

    assert body["timed"] == 0
    assert body["avg_duration_ms"] is None
    devto = next(p for p in body["by_platform"] if p["platform"] == "devto")
    assert devto["avg_duration_ms"] is None


def _timed(db, project, platform: Platform, duration_ms: int | None, *, n: int = 1):
    """*n* published rows on *platform*, each on a content row of its own.

    One per piece because ``uq_publication_content_platform`` allows exactly
    that, which is the constraint that makes a sample size mean something: ten
    Dev.to timings are ten posts, not one post ten times.
    """
    from app.models.publication import Publication

    for index in range(n):
        piece = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            title=f"{platform.value} {index}",
            slug=f"{platform.value}-{index}",
            body_markdown="word " * 50,
        )
        db.add(piece)
        db.flush()
        db.add(
            Publication(
                content_id=piece.id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                duration_ms=duration_ms,
            )
        )
    db.commit()


def test_the_overall_mean_is_weighted_by_attempts(db, user, project):
    """One Bluesky post and four hundred Dev.to posts are not two equal opinions.

    Written against the aggregate rather than through the API so the two
    platforms can be given wildly different sample sizes without publishing
    four hundred times.
    """
    _timed(db, project, Platform.DEVTO, 100, n=9)
    _timed(db, project, Platform.BLUESKY, 1100)

    payload = ops_metrics.publish_rates(db, user.id)

    assert payload["timed"] == 10
    # The mean of the means would be 600. Weighted, it is 200.
    assert payload["avg_duration_ms"] == 200


def test_an_untimed_row_is_not_averaged_as_zero(db, user, project):
    """``count`` and ``avg`` over the same nullable column agree by construction.

    Every publication written before this column existed is NULL, and a live
    install has a lot of them — folded in as zeros they would report the whole
    history as instant until enough new rows outweighed them.
    """
    _timed(db, project, Platform.DEVTO, 400)
    _timed(db, project, Platform.MEDIUM, None)

    payload = ops_metrics.publish_rates(db, user.id)

    assert payload["timed"] == 1
    assert payload["avg_duration_ms"] == 400
    medium = next(p for p in payload["by_platform"] if p["platform"] == "medium")
    assert medium["timed"] == 0
    assert medium["avg_duration_ms"] is None


def test_the_publication_is_shown_its_own_duration(
    db, client, auth, content, connected, succeeds
):
    """The per-row number, not just the aggregate.

    The aggregate answers "is this platform slow"; the row answers "was *this*
    post the slow one", which is the question somebody looking at a piece that
    took a minute to go out is actually asking.
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    detail = client.get(f"/api/v1/content/{content.id}", headers=auth).json()

    assert detail["publications"][0]["duration_ms"] == publication.duration_ms
