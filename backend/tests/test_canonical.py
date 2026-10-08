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
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.medium import MediumAdapter

DEVTO_URL = "https://dev.to/r2st/pulse-1-0"
MEDIUM_URL = "https://medium.com/@r2st/pulse-1-0"
BLUESKY_URL = "https://bsky.app/profile/r2st.bsky.social/post/3ms7qsatutl26"


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
    content.canonical_url = "https://pulse.example.com/blog/pulse-1-0"
    db.commit()

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url == "https://pulse.example.com/blog/pulse-1-0"


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


def test_a_social_post_never_becomes_the_canonical(db, user, content, monkeypatch):
    """The bug this fixes was live on the box for eight articles.

    Every autopilot project there has ``auto_canonical`` on and no
    ``canonical_platform`` named, so whichever destination published first took
    the title — and the batch was ordered alphabetically, so Bluesky beat Dev.to
    every time. The result: a full article on dev.to (and on the user's own
    blog) carrying ``rel=canonical`` to a 300-character post that links back to
    it, which tells a crawler the post is the original and the article is a copy.
    """
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.BLUESKY,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials(
                {"handle": "r2st.bsky.social", "app_password": "p"}
            ),
        )
    )
    db.commit()
    monkeypatch.setattr(
        BlueskyAdapter,
        "publish",
        lambda self, req, creds: PublishResult(
            external_id="at://x", external_url=BLUESKY_URL
        ),
    )

    publication = publishing_service.queue(db, content, ["bluesky"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.PUBLISHED
    assert content.canonical_url is None


def test_the_article_destination_is_queued_ahead_of_the_social_one(db, content):
    """Ordering, so the URL exists before the post that should point at it."""
    publications = publishing_service.queue(db, content, ["bluesky", "devto"])
    assert [p.platform for p in publications] == [Platform.DEVTO, Platform.BLUESKY]


def test_a_relative_or_empty_url_is_not_adopted(db, content, connected, monkeypatch):
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="9", external_url=""),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url is None


def test_a_url_longer_than_the_column_is_not_adopted(db, content, connected, monkeypatch):
    """``external_url`` is String(700); ``canonical_url`` is String(500).

    So a URL a platform returned and Pulse stored without complaint can still
    be one this column cannot hold, and this is the worst place in the tree for
    that to be discovered. The post is already live by the time ``_adopt_canonical``
    runs, and on PostgreSQL the over-long assignment fails the *same commit* that
    records the publication as PUBLISHED — leaving the piece on the platform and
    Pulse convinced it never went out, which is precisely the state the retry
    logic then tries to fix by publishing it again.

    Declining to adopt costs a rel=canonical the author can still type in. Every
    adapter already handles the field being empty.
    """
    long_url = "https://dev.to/r2st/" + "s" * 500
    assert len(long_url) > 500
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="9", external_url=long_url),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url is None
    # The publish itself still succeeded — declining the canonical is not a
    # failure of the thing the user asked for.
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.external_url == long_url


def test_a_url_that_exactly_fills_the_column_is_still_adopted(
    db, content, connected, monkeypatch
):
    """The boundary, so the guard cannot quietly become "shorter than 500"."""
    exact = "https://dev.to/r2st/" + "s" * (500 - len("https://dev.to/r2st/"))
    assert len(exact) == 500
    monkeypatch.setattr(
        DevToAdapter,
        "publish",
        lambda self, req, creds: PublishResult(external_id="9", external_url=exact),
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)

    assert content.canonical_url == exact


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


def test_the_implicit_original_is_staggered_like_a_designated_one(db, content):
    """No named platform still has an original: whichever article host wins.

    ``_adopt_canonical`` hands the canonical URL to the first article
    destination that publishes, so there *is* an order to enforce even with the
    project's ``canonical_platform`` unset — and the default project has it
    unset. Leaving this batch unstaggered was what let the copies go out before
    the original had a URL for them to point at.
    """
    publications = _by_platform(
        publishing_service.queue(db, content, ["devto", "medium"])
    )

    assert publications[Platform.DEVTO].status == PublicationStatus.PENDING
    assert publications[Platform.DEVTO].scheduled_for is None
    assert publications[Platform.MEDIUM].status == PublicationStatus.SCHEDULED
    assert publications[Platform.MEDIUM].scheduled_for is not None


def test_nothing_is_staggered_when_the_project_opts_out(db, project, content):
    """``auto_canonical`` off means no URL is adopted, so nothing is waiting."""
    project.auto_canonical = False
    db.commit()

    publications = publishing_service.queue(db, content, ["devto", "medium"])
    assert all(p.scheduled_for is None for p in publications)
    assert all(p.status == PublicationStatus.PENDING for p in publications)


def test_nothing_is_staggered_among_destinations_that_host_no_article(db, content):
    """A batch of social posts has no original among it to wait for."""
    publications = publishing_service.queue(db, content, ["bluesky", "mastodon"])
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


def test_the_users_own_domain_outranks_someone_elses_platform(db, content):
    """With no platform named, the copy on the user's own site is the original.

    Both Dev.to and a Git destination host the article, so both could hold the
    canonical — but one of them is the user's own domain and the other is a
    syndicated copy. Alphabetical order put Dev.to first, which pointed the
    project's own blog post at dev.to as its original.
    """
    publications = publishing_service.queue(db, content, ["devto", "git"])

    assert [p.platform for p in publications] == [Platform.GIT, Platform.DEVTO]
    by_platform = _by_platform(publications)
    assert by_platform[Platform.GIT].scheduled_for is None
    assert by_platform[Platform.DEVTO].status == PublicationStatus.SCHEDULED


def test_a_named_canonical_still_wins_over_the_owned_domain(db, project, content):
    """An explicit answer beats an inferred one, as everywhere else here."""
    project.canonical_platform = Platform.DEVTO
    db.commit()

    publications = publishing_service.queue(db, content, ["devto", "git"])

    assert publications[0].platform == Platform.DEVTO
