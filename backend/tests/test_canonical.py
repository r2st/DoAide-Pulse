"""Auto-canonical URLs and syndication order.

The whole point of ``canonical_url`` is that a syndicated copy does not compete
with the original. Every adapter already sent the field; nothing set it. These
tests pin down what sets it, what must *not*, and the ordering that makes it
possible for a copy to carry one at all.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, PublicationStatus
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import PublishResult
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.medium import MediumAdapter

DEVTO_URL = "https://dev.to/r2st/herald-1-0"
MEDIUM_URL = "https://medium.com/@r2st/herald-1-0"


def _by_platform(publications):
    """Index a queue result by platform.

    ``queue`` returns the canonical platform first, so positional unpacking
    silently reorders as soon as a test sets ``canonical_platform``.
    """
    return {p.platform: p for p in publications}


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
    """Live credentials for both implemented platforms."""
    db.add_all(
        [
            PlatformConnection(
                user_id=user.id,
                platform=Platform.DEVTO,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"api_key": "k"}),
            ),
            PlatformConnection(
                user_id=user.id,
                platform=Platform.MEDIUM,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"integration_token": "t"}),
            ),
        ]
    )
    db.commit()


@pytest.fixture
def adapters(monkeypatch):
    """Both adapters succeed with a distinguishable URL."""
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="1", external_url=DEVTO_URL),
    )
    monkeypatch.setattr(
        MediumAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="2", external_url=MEDIUM_URL),
    )


# --------------------------------------------------------------------------- #
# Adopting the URL                                                             #
# --------------------------------------------------------------------------- #


def test_first_publish_becomes_the_canonical(db, content, connected, adapters):
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert content.canonical_url == DEVTO_URL


def test_the_second_platform_is_told_about_the_first(
    db, content, connected, monkeypatch
):
    """The copy has to actually receive the original's URL, not merely coexist."""
    sent: list[str | None] = []

    def _capture(self, request, credentials):
        sent.append(request.canonical_url)
        return PublishResult(external_id="2", external_url=MEDIUM_URL)

    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="1", external_url=DEVTO_URL),
    )
    monkeypatch.setattr(MediumAdapter, "publish", _capture)

    queued = _by_platform(publishing_service.queue(db, content, ["devto", "medium"]))
    db.commit()
    publishing_service.execute(db, queued[Platform.DEVTO])
    publishing_service.execute(db, queued[Platform.MEDIUM])

    assert sent == [DEVTO_URL]
    # And the original keeps ownership — a later publish never overwrites it.
    assert content.canonical_url == DEVTO_URL


def test_a_hand_typed_canonical_is_never_overwritten(db, content, connected, adapters):
    content.canonical_url = "https://herald.example.com/blog/herald-1-0"
    db.commit()

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url == "https://herald.example.com/blog/herald-1-0"


def test_opting_out_leaves_the_field_empty(db, project, content, connected, adapters):
    project.auto_canonical = False
    db.commit()

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url is None


def test_only_the_designated_platform_may_claim_it(
    db, project, content, connected, adapters
):
    """Dev.to publishing first must not make itself the original."""
    project.canonical_platform = Platform.MEDIUM
    db.commit()

    queued = _by_platform(publishing_service.queue(db, content, ["devto", "medium"]))
    db.commit()

    publishing_service.execute(db, queued[Platform.DEVTO])
    assert content.canonical_url is None

    publishing_service.execute(db, queued[Platform.MEDIUM])
    assert content.canonical_url == MEDIUM_URL


def test_a_staged_draft_never_becomes_the_canonical(db, content, connected, monkeypatch):
    """A draft's URL is a private editor link — it would 404 for a crawler."""
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(
            external_id="7", external_url="https://dev.to/dashboard/7"
        ),
    )

    publication = publishing_service.queue(db, content, ["devto"], as_draft=True)[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert content.canonical_url is None


def test_a_relative_or_empty_url_is_not_adopted(db, content, connected, monkeypatch):
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="9", external_url=""),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url is None


# --------------------------------------------------------------------------- #
# Syndication order                                                            #
# --------------------------------------------------------------------------- #


def test_copies_wait_behind_the_designated_original(db, project, content):
    project.canonical_platform = Platform.DEVTO
    db.commit()

    publications = _by_platform(
        publishing_service.queue(db, content, ["devto", "medium"])
    )

    assert publications[Platform.DEVTO].status == PublicationStatus.PENDING
    assert publications[Platform.DEVTO].scheduled_for is None
    medium = publications[Platform.MEDIUM]
    assert medium.status == PublicationStatus.SCHEDULED
    assert medium.scheduled_for is not None


def test_the_stagger_is_measured_from_an_explicit_schedule(db, project, content):
    """A batch scheduled for 9am publishes the original then, copies after."""
    project.canonical_platform = Platform.DEVTO
    db.commit()
    when = datetime.now(UTC) + timedelta(days=1)

    publications = _by_platform(
        publishing_service.queue(db, content, ["devto", "medium"], scheduled_for=when)
    )

    from app.config import settings

    assert publications[Platform.DEVTO].scheduled_for == when
    assert publications[Platform.MEDIUM].scheduled_for == when + timedelta(
        seconds=settings.syndication_delay_seconds
    )


def test_the_canonical_platform_is_queued_first(db, project, content):
    """Ordering matters even with the delay off — the caller dispatches in order."""
    project.canonical_platform = Platform.MEDIUM
    db.commit()

    publications = publishing_service.queue(db, content, ["devto", "medium"])
    assert publications[0].platform == Platform.MEDIUM


def test_nothing_is_staggered_without_a_designated_original(db, content):
    """With no primary there is no order to enforce: first to finish wins."""
    publications = publishing_service.queue(db, content, ["devto", "medium"])
    assert all(p.scheduled_for is None for p in publications)
    assert all(p.status == PublicationStatus.PENDING for p in publications)


def test_a_single_platform_is_never_delayed(db, project, content):
    project.canonical_platform = Platform.DEVTO
    db.commit()

    publication = publishing_service.queue(db, content, ["devto"])[0]
    assert publication.scheduled_for is None


def test_syndicating_after_the_fact_does_not_wait(db, project, content):
    """Once the original's URL is known there is nothing left to wait for."""
    project.canonical_platform = Platform.DEVTO
    content.canonical_url = DEVTO_URL
    db.commit()

    publication = publishing_service.queue(db, content, ["medium"])[0]
    assert publication.scheduled_for is None


def test_a_zero_delay_publishes_everything_at_once(db, project, content, monkeypatch):
    monkeypatch.setattr(
        "app.services.publishing_service.settings.syndication_delay_seconds", 0
    )
    project.canonical_platform = Platform.DEVTO
    db.commit()

    publications = publishing_service.queue(db, content, ["devto", "medium"])
    assert all(p.scheduled_for is None for p in publications)
    # The ordering guarantee survives, which is all that is left to give.
    assert publications[0].platform == Platform.DEVTO
