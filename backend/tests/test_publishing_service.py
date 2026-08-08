"""Queueing, executing and status derivation — without touching a platform."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import (
    CredentialError,
    PublishError,
    PublishResult,
    RateLimited,
)


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
        tags=["python"],
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


def test_queue_creates_one_row_per_platform(db, content):
    publications = publishing_service.queue(db, content, ["devto", "medium"])
    assert {p.platform for p in publications} == {Platform.DEVTO, Platform.MEDIUM}
    # Only the original goes out now — the copy waits behind it, so that it can
    # carry a canonical link to a URL that exists. See test_canonical.py.
    by_platform = {p.platform: p for p in publications}
    assert by_platform[Platform.DEVTO].status == PublicationStatus.PENDING
    assert by_platform[Platform.MEDIUM].status == PublicationStatus.SCHEDULED


def test_requeue_rearms_rather_than_duplicating(db, content):
    first = publishing_service.queue(db, content, ["devto"])[0]
    first.status = PublicationStatus.FAILED
    first.attempts = 3
    first.error = "boom"
    db.commit()

    again = publishing_service.queue(db, content, ["devto"])[0]

    assert again.id == first.id
    assert again.status == PublicationStatus.PENDING
    assert again.attempts == 0
    assert again.error is None
    assert db.query(Publication).count() == 1


def test_requeue_will_not_repost_something_live(db, content):
    first = publishing_service.queue(db, content, ["devto"])[0]
    first.status = PublicationStatus.PUBLISHED
    db.commit()

    again = publishing_service.queue(db, content, ["devto"])[0]
    assert again.status == PublicationStatus.PUBLISHED


def test_as_draft_reaches_the_adapter(db, content, connected, monkeypatch):
    """The flag has to survive the trip from the request to the adapter.

    It used to be read off the payload and dropped, so "stage it on the
    platform" published live — the one failure mode this option exists to
    prevent.
    """
    from app.services.publishers.devto import DevToAdapter

    seen: list[bool] = []

    def _capture(self, request, credentials):
        seen.append(request.as_draft)
        return PublishResult(external_id="1", external_url="u")

    monkeypatch.setattr(DevToAdapter, "publish", _capture)

    publication = publishing_service.queue(db, content, ["devto"], as_draft=True)[0]
    db.commit()
    assert publication.as_draft is True

    publishing_service.execute(db, publication)
    assert seen == [True]


def test_as_draft_survives_a_scheduled_publish(db, content):
    """A beat sweep executing this row hours later has only the row to go on."""
    when = datetime.now(UTC) + timedelta(days=1)
    publication = publishing_service.queue(
        db, content, ["devto"], scheduled_for=when, as_draft=True
    )[0]
    db.commit()

    db.expire_all()
    reloaded = publishing_service.due_publications(db, now=when + timedelta(minutes=1))
    assert [p.as_draft for p in reloaded] == [True]
    assert reloaded[0].id == publication.id


def test_requeue_resets_as_draft_to_the_new_choice(db, content):
    first = publishing_service.queue(db, content, ["devto"], as_draft=True)[0]
    db.commit()
    again = publishing_service.queue(db, content, ["devto"])[0]
    assert again.id == first.id
    assert again.as_draft is False


def test_scheduled_queue_marks_scheduled(db, content):
    when = datetime.now(UTC) + timedelta(days=1)
    publication = publishing_service.queue(db, content, ["devto"], scheduled_for=when)[0]
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for == when


def test_execute_without_a_connection_fails_terminally(db, content):
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert "Not connected" in publication.error or "connection" in publication.error


def test_successful_publish_updates_both_rows(db, content, connected, monkeypatch):
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(
            external_id="42", external_url="https://dev.to/r2st/herald-1-0"
        ),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.external_url == "https://dev.to/r2st/herald-1-0"
    assert publication.published_at is not None
    # One success is enough to call the piece published.
    assert content.status == ContentStatus.PUBLISHED


def test_one_platform_succeeding_is_enough(db, user, content, connected, monkeypatch):
    from app.services.publishers.devto import DevToAdapter
    from app.services.publishers.medium import MediumAdapter

    # Both platforms connected: the point of this test is that a *publish*
    # failure on one is survivable, not that a missing connection is.
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.MEDIUM,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"integration_token": "t"}),
        )
    )
    db.commit()

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="1", external_url="u"),
    )
    monkeypatch.setattr(
        MediumAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(PublishError("medium is down")),
    )

    devto, medium = publishing_service.queue(db, content, ["devto", "medium"])
    db.commit()
    publishing_service.execute(db, devto)
    publishing_service.execute(db, medium)

    assert content.status == ContentStatus.PUBLISHED
    assert medium.status == PublicationStatus.PENDING  # retryable, not terminal


def test_every_platform_failing_fails_the_content(db, content, connected, monkeypatch):
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(CredentialError("bad key")),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert content.status == ContentStatus.FAILED
    # A rejected credential invalidates the connection so the UI can say so.
    db.refresh(connected)
    assert connected.status == ConnectionStatus.INVALID


def test_retryable_failure_becomes_terminal_after_the_cap(
    db, content, connected, monkeypatch
):
    from app.config import settings
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(PublishError("timeout")),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    for _ in range(settings.publish_max_retries - 1):
        publishing_service.execute(db, publication)
        assert publication.status == PublicationStatus.PENDING

    publishing_service.execute(db, publication)
    assert publication.status == PublicationStatus.FAILED
    assert publication.attempts == settings.publish_max_retries


def test_due_publications_respects_the_schedule(db, content):
    now = datetime.now(UTC)
    immediate = publishing_service.queue(db, content, ["devto"])[0]
    future = publishing_service.queue(
        db, content, ["medium"], scheduled_for=now + timedelta(days=1)
    )[0]
    db.commit()

    due = publishing_service.due_publications(db, now=now)
    assert immediate in due
    assert future not in due

    due_later = publishing_service.due_publications(db, now=now + timedelta(days=2))
    assert future in due_later


# --------------------------------------------------------------------------- #
# Rate limiting                                                               #
# --------------------------------------------------------------------------- #


def _rate_limit(retry_after):
    def _raise(self, req, creds):
        raise RateLimited("Dev.to rate-limited the request", retry_after=retry_after)

    return _raise


def test_rate_limited_publish_waits_for_the_platforms_own_window(
    db, content, connected, monkeypatch
):
    """A 429 parks the row until the platform said to come back.

    Leaving it ``pending`` would hand it straight back to the next sweep, which
    is minutes away — precisely the behaviour that turns a soft limit into a
    ban.
    """
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(DevToAdapter, "publish", _rate_limit(600.0))

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = datetime.now(UTC)
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for is not None
    delay = as_aware(publication.scheduled_for) - before
    assert timedelta(seconds=590) <= delay <= timedelta(seconds=610)
    assert "retrying in 600s" in publication.error
    # Not due yet, so the sweep leaves it alone.
    assert publication not in publishing_service.due_publications(db, now=before)


def test_rate_limit_without_guidance_falls_back_to_the_sweep_interval(
    db, content, connected, monkeypatch
):
    from app.config import settings
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(DevToAdapter, "publish", _rate_limit(None))

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = datetime.now(UTC)
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.SCHEDULED
    delay = as_aware(publication.scheduled_for) - before
    assert delay <= timedelta(seconds=settings.publish_scan_interval_seconds + 5)


def test_an_absurd_retry_after_is_capped(db, content, connected, monkeypatch):
    """A post that vanishes for a week reads as a bug, not as a queue."""
    from app.config import settings
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(DevToAdapter, "publish", _rate_limit(7 * 86400.0))

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = datetime.now(UTC)
    publishing_service.execute(db, publication)

    delay = as_aware(publication.scheduled_for) - before
    assert delay <= timedelta(
        seconds=settings.publish_rate_limit_max_defer_seconds + 5
    )


def test_relentless_rate_limiting_still_ends_up_in_front_of_a_human(
    db, content, connected, monkeypatch
):
    from app.config import settings
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(DevToAdapter, "publish", _rate_limit(30.0))

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    for _ in range(settings.publish_max_retries - 1):
        publishing_service.execute(db, publication)
        assert publication.status == PublicationStatus.SCHEDULED

    publishing_service.execute(db, publication)
    assert publication.status == PublicationStatus.FAILED
    assert content.status == ContentStatus.FAILED


# -- Bounded failure messages ---------------------------------------------- #
#
# Adapter messages quote what the platform said, and several quote the whole
# body when it is not the shape they expected — `f"Hashnode returned no post:
# {data}"`. That body is not ours and has no size limit; `error` is a `Text`
# column with none either, it is rewritten on every attempt, and the
# publications list renders it.


def test_a_vast_upstream_error_is_clipped_before_it_is_stored(
    db, content, connected, monkeypatch
):
    from app.services.publishers.devto import DevToAdapter

    huge = "x" * 50_000
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(CredentialError(huge)),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert len(publication.error) == publishing_service.MAX_ERROR_CHARS
    assert publication.error.endswith("…")


def test_the_connection_note_is_clipped_too(db, content, connected, monkeypatch):
    """`last_error` is the same unbounded write, shown on the settings page."""
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(CredentialError("y" * 50_000)),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    db.refresh(connected)
    assert len(connected.last_error) == publishing_service.MAX_ERROR_CHARS


def test_an_ordinary_error_is_stored_whole(db, content, connected, monkeypatch):
    """Clipping must not touch the messages anyone actually reads."""
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(CredentialError("bad key")),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.error == "bad key"


def test_a_clipped_rate_limit_note_still_says_when_it_retries(
    db, content, connected, monkeypatch
):
    """The deferral message appends the wait — clipping must not eat it.

    It is built as `f"{exc} — retrying in {n}s"`, so a vast `exc` would push the
    only actionable part of the sentence off the end.
    """
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: (_ for _ in ()).throw(
            RateLimited("z" * 50_000, retry_after=30)
        ),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.SCHEDULED
    assert len(publication.error) == publishing_service.MAX_ERROR_CHARS
