"""The breaker in front of the platforms: what opens it, and what it protects.

``base.Adapter._request`` answers the blip and ``_fail``/``_defer`` answer the
outage — for *one* row. Neither can see the second row, and a queue of thirty
against a platform having a bad twenty minutes is thirty rows independently
rediscovering the same fact, one attempt at a time, until every one of them is
terminally failed with nothing wrong with any of them.

So what these pin is mostly about the *second* row. The first one is the canary
and keeps the escalation it has always had; the ones behind it are held without
spending an attempt. And they pin, at least as carefully, what must **not** open
the breaker: a rejected token, an unfinished adapter, an unsupported option and
a missing connection are all facts about one request that a perfectly healthy
platform states the same way every time, and a breaker that counted them would
shut a working route and park the rows that would have gone out.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers import breaker
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
    PublishResult,
    RateLimited,
    UnsupportedOption,
)


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="pulse-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Pulse 1.0 is out.",
        meta_description="Pulse 1.0 is out.",
        tags=["python"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    for platform in (Platform.DEVTO, Platform.BLUESKY, Platform.GIT):
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"api_key": "k"}),
                display_name=f"@r2st-{platform.value}",
            )
        )
    db.commit()


def _raise(exc):
    """An adapter ``publish`` that always fails the same way.

    Patched onto the adapter *class* rather than onto the singleton instance,
    which is the convention the rest of the suite already follows and not merely
    a style: ``monkeypatch.setattr(instance, "publish", ...)`` records the bound
    method it shadowed and puts it *back as an instance attribute* on undo. The
    singleton then carries a permanent shadow, and every later test that patches
    the class — which is all of them — silently patches something no longer
    reached.
    """

    def _publish(self, request, credentials):
        raise exc

    return _publish


def _fresh(db, content, platform=Platform.DEVTO) -> Publication:
    """A publication nothing has tried yet — the row the breaker is for.

    Replaces any existing row for the pair, because ``(content, platform)`` is
    unique and these tests want a *stream* of untried rows against one platform:
    that is the queue the breaker exists to protect, and building it out of one
    content row per publication would say nothing extra.
    """
    existing = (
        db.query(Publication)
        .filter_by(content_id=content.id, platform=platform)
        .one_or_none()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()
    row = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PENDING,
        attempts=0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fail_n_times(db, content, n: int) -> None:
    """Put *n* untried rows through the platform, one after another."""
    for _ in range(n):
        publishing_service.execute(db, _fresh(db, content))


# --------------------------------------------------------------------------- #
# What opens it                                                                #
# --------------------------------------------------------------------------- #


def test_consecutive_transient_failures_open_the_route(db, user, content, connected, monkeypatch):
    """Four blips in a row is not four blips, it is a platform."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502 from the edge")))

    _fail_n_times(db, content, settings.publish_breaker_threshold)

    assert breaker.is_open(Platform.DEVTO, user.id)


def test_one_failure_does_not(db, user, content, connected, monkeypatch):
    """A platform that fails one request in ten is degraded, not down."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502 from the edge")))

    publishing_service.execute(db, _fresh(db, content))

    assert not breaker.is_open(Platform.DEVTO, user.id)


def test_a_rate_limit_opens_it_for_the_window_the_platform_named(
    db, user, content, connected, monkeypatch
):
    """No threshold to wait for: the platform has already said it will not serve
    this account, and two more requests to confirm that is what the limit is
    there to stop."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=120))
    )

    publishing_service.execute(db, _fresh(db, content))

    assert breaker.is_open(Platform.DEVTO, user.id)
    assert 110 < breaker.seconds_remaining(Platform.DEVTO, user.id) <= 120


def test_an_absurd_retry_after_is_capped(db, user, content, connected, monkeypatch):
    """A platform naming a week must not take the route out for a week."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=604800))
    )

    publishing_service.execute(db, _fresh(db, content))

    assert breaker.seconds_remaining(Platform.DEVTO, user.id) <= (
        settings.publish_breaker_max_cooldown_seconds
    )


def test_a_success_forgets_the_failures_before_it(db, user, content, connected, monkeypatch):
    """Discarded rather than decremented: a half-remembered outage from an hour
    ago would trip the breaker early on the next unrelated blip."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502")))
    _fail_n_times(db, content, settings.publish_breaker_threshold - 1)

    monkeypatch.setattr(
        type(adapter),
        "publish",
        lambda self, request, credentials: PublishResult(
            external_id="1", external_url="https://dev.to/r2st/pulse-1-0"
        ),
    )
    publishing_service.execute(db, _fresh(db, content))

    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502")))
    _fail_n_times(db, content, 1)

    assert not breaker.is_open(Platform.DEVTO, user.id)


# --------------------------------------------------------------------------- #
# What must not open it                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        CredentialError("Dev.to rejected the stored API key."),
        NotImplementedAdapter("no adapter yet"),
        UnsupportedOption("Bluesky has no drafts"),
    ],
    ids=["credential", "unimplemented", "unsupported"],
)
def test_a_fact_about_the_request_never_shuts_the_route(
    db, user, content, connected, monkeypatch, exc
):
    """Each of these is what a *working* platform says about one bad request.

    Counting them would open a breaker on a healthy route and then park every
    row behind it — turning one user's expired token into an outage for the
    whole queue.
    """
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(exc))

    for _ in range(settings.publish_breaker_threshold + 2):
        publishing_service.execute(db, _fresh(db, content))
        # A credential failure marks the connection invalid, which would make
        # every later attempt a NotConnected instead. Put it back: the point of
        # this test is the breaker, not the connection.
        connection = db.query(PlatformConnection).filter_by(
            user_id=user.id, platform=Platform.DEVTO
        ).one()
        connection.status = ConnectionStatus.CONNECTED
        db.commit()

    assert not breaker.is_open(Platform.DEVTO, user.id)


def test_a_missing_connection_never_shuts_the_route(db, user, content):
    """The ten production rows, over and over. Nothing was ever sent, so nothing
    was learned about the platform."""
    _fail_n_times(db, content, settings.publish_breaker_threshold + 2)

    assert not breaker.is_open(Platform.DEVTO, user.id)


# --------------------------------------------------------------------------- #
# What it protects, and what it deliberately does not                          #
# --------------------------------------------------------------------------- #


def test_a_fresh_row_is_held_without_spending_an_attempt(
    db, user, content, connected, monkeypatch
):
    """The whole point. The queue behind the outage keeps its retries."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502")))
    _fail_n_times(db, content, settings.publish_breaker_threshold)

    sent: list[object] = []
    monkeypatch.setattr(
        type(adapter), "publish", lambda self, request, credentials: sent.append(request)
    )
    held = _fresh(db, content)
    publishing_service.execute(db, held)

    db.refresh(held)
    assert sent == []  # nothing reached the platform
    assert held.attempts == 0  # and nothing was charged for it
    assert held.status == PublicationStatus.SCHEDULED
    assert held.scheduled_for is not None
    # The publications list renders this column; a row that silently moved into
    # the future with nothing beside it is a state an operator cannot read.
    assert held.error == publishing_service.BREAKER_OPEN_ERROR


def test_the_hold_lasts_as_long_as_the_route_is_shut(
    db, user, content, connected, monkeypatch
):
    """Parked for the breaker's own window, not for a backoff of the row's
    invention — a row that comes back sooner only re-parks itself, one dispatch
    at a time, for the whole cooldown."""
    monkeypatch.setattr(settings, "publish_scan_interval_seconds", 10)
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=900))
    )
    publishing_service.execute(db, _fresh(db, content))

    held = _fresh(db, content)
    publishing_service.execute(db, held)

    db.refresh(held)
    from app.models.mixins import as_aware, utcnow

    wait = (as_aware(held.scheduled_for) - utcnow()).total_seconds()
    assert 800 < wait <= 900


def test_a_row_already_in_the_retry_cycle_is_let_through(
    db, user, content, connected, monkeypatch
):
    """The canary keeps its escalation.

    ``_fail`` and ``_defer`` guarantee that a platform failing every attempt
    ends up terminal and in front of a human. A breaker able to hold *any* row
    would repeal that quietly, leaving a queue waiting forever on an outage
    nobody is told about — so the row that met the outage first is never held.
    """
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502")))
    _fail_n_times(db, content, settings.publish_breaker_threshold)
    assert breaker.is_open(Platform.DEVTO, user.id)

    canary = _fresh(db, content)
    canary.attempts = 1  # already tried once
    db.commit()
    publishing_service.execute(db, canary)

    db.refresh(canary)
    assert canary.attempts == 2  # it was tried, not held
    assert canary.error != publishing_service.BREAKER_OPEN_ERROR


def test_a_relentless_platform_still_reaches_a_human(
    db, user, content, connected, monkeypatch
):
    """The invariant the asymmetry above exists to keep, stated end to end."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(adapter), "publish", _raise(PublishError("502")))
    publication = _fresh(db, content)

    for _ in range(settings.publish_max_retries):
        publishing_service.execute(db, publication)

    db.refresh(publication)
    assert publication.status == PublicationStatus.FAILED
    assert publication.error != publishing_service.BREAKER_OPEN_ERROR


# --------------------------------------------------------------------------- #
# Tenancy                                                                      #
# --------------------------------------------------------------------------- #


def test_one_accounts_bad_afternoon_does_not_park_anothers_queue(
    db, user, content, connected, monkeypatch
):
    """The reason the key is per account.

    A rate limit is charged against an API key, so it is one account's fact. A
    breaker keyed on the platform alone would let one busy account park every
    other account's posts — a cross-tenant denial of service Pulse would be
    doing to itself.
    """
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=900))
    )
    publishing_service.execute(db, _fresh(db, content))
    assert breaker.is_open(Platform.DEVTO, user.id)

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()

    assert not breaker.is_open(Platform.DEVTO, other.id)


def test_one_platform_going_down_does_not_take_the_others_with_it(
    db, user, content, connected, monkeypatch
):
    devto = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(type(devto), "publish", _raise(RateLimited("slow", retry_after=900)))
    publishing_service.execute(db, _fresh(db, content))

    assert breaker.is_open(Platform.DEVTO, user.id)
    assert not breaker.is_open(Platform.BLUESKY, user.id)
    assert not breaker.is_open(Platform.GIT, user.id)


# --------------------------------------------------------------------------- #
# The switch                                                                   #
# --------------------------------------------------------------------------- #


def test_the_breaker_can_be_switched_off(db, user, content, connected, monkeypatch):
    """A new layer in front of every publish is a layer worth being able to
    turn off from the environment when it is the thing that is wrong."""
    monkeypatch.setattr(settings, "publish_breaker_enabled", False)
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=900))
    )

    _fail_n_times(db, content, settings.publish_breaker_threshold + 2)

    assert not breaker.is_open(Platform.DEVTO, user.id)


def test_the_snapshot_names_the_route(db, user, content, connected, monkeypatch):
    """``devto:7`` — greppable, and what the diagnostics payload renders."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=900))
    )
    publishing_service.execute(db, _fresh(db, content))

    snapshot = breaker.snapshot()

    assert f"devto:{user.id}" in snapshot
    assert snapshot[f"devto:{user.id}"]["seconds_until_retry"] > 0


def test_a_piece_held_by_the_breaker_is_not_called_failed(
    db, user, content, connected, monkeypatch
):
    """Nothing failed and nothing was sent. A piece whose only publication is
    waiting for a platform to come back must not read ``failed`` in the UI."""
    adapter = publishing_service.publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        type(adapter), "publish", _raise(RateLimited("slow down", retry_after=900))
    )
    publishing_service.execute(db, _fresh(db, content))

    held_content = Content(
        project_id=content.project_id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.1",
        slug="pulse-1-1",
        body_markdown="## Also out\n\n" + ("word " * 200),
        excerpt="Pulse 1.1 is out.",
        meta_description="Pulse 1.1 is out.",
        status=ContentStatus.APPROVED,
        tags=["python"],
    )
    db.add(held_content)
    db.commit()
    db.refresh(held_content)
    publishing_service.execute(db, _fresh(db, held_content))

    db.refresh(held_content)
    assert held_content.status == ContentStatus.APPROVED
