"""What a platform returns has to fit the columns that record it.

``external_id`` and ``external_url`` are written straight from a response body.
Dev.to and Medium answer with their own permalinks, but WordPress, Mastodon and
Bluesky are servers the *user* typed the address of — the same untrusted-response
problem the SSRF guard covers the request side of — and nothing bounds what one
of them puts in a ``link`` field.

The reason that matters more here than anywhere else: the assignment happens
between the platform accepting the post and the commit that records it. On
PostgreSQL an over-long value raises ``StringDataRightTruncation`` from that
commit, so the post is live, the row never reaches ``published``, and the reclaim
sweep re-arms it — publishing the same piece a second time. Dropping the field
costs a link; keeping it costs a duplicate post.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import (
    EXTERNAL_ID_MAX_LENGTH,
    EXTERNAL_URL_MAX_LENGTH,
    Platform,
    PublicationStatus,
)
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import PublishResult
from app.services.publishers.devto import DevToAdapter


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Pulse 1.0 is out.",
        meta_description="Pulse 1.0 is out.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()


def _publishes(monkeypatch, *, external_id: str, external_url: str) -> None:
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(
            external_id=external_id, external_url=external_url
        ),
    )


def test_a_permalink_too_long_for_the_column_is_dropped_not_stored(
    db, content, connected, monkeypatch
):
    """The publish stands; the link is what gives way.

    SQLite does not enforce ``String(n)``, so the assertion that matters is on
    the value rather than on an exception: what is stored has to be something
    PostgreSQL would have taken.
    """
    long_url = "https://example.com/" + "s" * EXTERNAL_URL_MAX_LENGTH
    _publishes(monkeypatch, external_id="9", external_url=long_url)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.external_url is None
    # The id is the half that still identifies the post, so it is kept.
    assert publication.external_id == "9"


def test_a_permalink_that_exactly_fills_the_column_is_kept(
    db, content, connected, monkeypatch
):
    """The boundary, so the guard cannot quietly become "shorter than 700"."""
    prefix = "https://example.com/"
    exact = prefix + "s" * (EXTERNAL_URL_MAX_LENGTH - len(prefix))
    assert len(exact) == EXTERNAL_URL_MAX_LENGTH
    _publishes(monkeypatch, external_id="9", external_url=exact)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert publication.external_url == exact


def test_a_post_id_too_long_for_the_column_is_dropped_not_truncated(
    db, content, connected, monkeypatch
):
    """A truncated id is worse than none: it fetches somebody else's metrics.

    Dropping it costs this post its engagement numbers — the metrics sweep
    selects on ``external_id IS NOT NULL`` — and leaves everything else about
    the publication correct.
    """
    _publishes(
        monkeypatch,
        external_id="9" * (EXTERNAL_ID_MAX_LENGTH + 1),
        external_url="https://example.com/post",
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.external_id is None
    assert publication.external_url == "https://example.com/post"


def test_the_piece_still_reads_as_published(db, content, connected, monkeypatch):
    """Neither field is what makes a piece live, and dropping one cannot change that."""
    _publishes(
        monkeypatch,
        external_id="9" * (EXTERNAL_ID_MAX_LENGTH + 1),
        external_url="https://example.com/" + "s" * EXTERNAL_URL_MAX_LENGTH,
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)
    db.refresh(content)

    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.published_at is not None
    assert publication.error is None
    assert content.status.value == "published"
    # An over-long URL is not adopted as the canonical either — it never was,
    # but it must not arrive there through the dropped field as an empty string.
    assert content.canonical_url is None
