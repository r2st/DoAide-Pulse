"""The early returns in the publishing service — the ones that stop work.

Each of these is a guard whose whole job is to *not* do something: not stagger a
syndication that has already been anchored, not write a metric for a platform
that has no metrics, not mark a content row failed on the strength of no
publications at all. They are cheap to get wrong and expensive to notice,
because the symptom is always something that quietly did not happen.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service
from app.services.publishers.base import PublishError, RateLimited


def _content(db, project, **kwargs) -> Content:
    row = Content(
        project_id=project.id,
        content_type=kwargs.pop("content_type", ContentType.TUTORIAL),
        title=kwargs.pop("title", "A post"),
        slug=kwargs.pop("slug", "a-post"),
        status=kwargs.pop("status", ContentStatus.APPROVED),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _publication(db, content, **kwargs) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=kwargs.pop("platform", Platform.DEVTO),
        status=kwargs.pop("status", PublicationStatus.PUBLISHED),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# Syndication stagger                                                         #
# --------------------------------------------------------------------------- #


def test_a_piece_that_already_has_its_canonical_url_is_not_staggered(db, project):
    """The stagger buys time for the original's URL to exist.

    When ``canonical_url`` is already set — a re-publish, or a piece written
    against a URL the author supplied — the copies have everything they need to
    point at it, and delaying them by the syndication window buys nothing and
    costs the user a visibly late cross-post.
    """
    content = _content(db, project, canonical_url="https://blog.example.com/a-post")
    db.commit()

    stagger = publishing_service._syndication_schedule(
        content, [Platform.DEVTO, Platform.HASHNODE, Platform.MEDIUM], base=None
    )

    assert stagger == {}


def test_a_piece_without_a_canonical_url_staggers_the_copies(db, project, monkeypatch):
    """The same call, with the URL missing, is where the delay belongs."""
    monkeypatch.setattr(settings, "syndication_delay_seconds", 900)
    content = _content(db, project, canonical_url=None)
    db.commit()
    base = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    stagger = publishing_service._syndication_schedule(
        content, [Platform.DEVTO, Platform.HASHNODE, Platform.MEDIUM], base=base
    )

    assert stagger
    assert all(when == base + timedelta(seconds=900) for when in stagger.values())


# --------------------------------------------------------------------------- #
# Marking a connection invalid                                                #
# --------------------------------------------------------------------------- #


def test_marking_a_connection_invalid_when_there_is_no_connection_is_a_no_op(db, user):
    """A publish can outlive the connection it was authorised by.

    The user disconnects the platform while a queued publication is in flight;
    the adapter answers 401; the handler reaches for a row that is no longer
    there. Raising here would turn a handled auth failure into an unhandled
    exception inside the worker, losing the error message that explains it.
    """
    publishing_service._mark_connection_invalid(
        db, user.id, Platform.DEVTO, "401 Unauthorized"
    )

    db.commit()  # nothing to write, but the caller commits regardless


# --------------------------------------------------------------------------- #
# Deriving the content status                                                 #
# --------------------------------------------------------------------------- #


def test_content_with_no_publications_keeps_its_status(db, project):
    """``all([])`` is True, so without the guard a piece routed nowhere would be
    marked FAILED the moment anything called into this — including the
    approval path, which has no publications yet by definition."""
    content = _content(db, project, status=ContentStatus.APPROVED)
    db.commit()

    publishing_service.sync_content_status(content)

    assert content.status == ContentStatus.APPROVED


def test_one_success_among_failures_still_publishes_the_piece(db, project):
    content = _content(db, project, status=ContentStatus.APPROVED)
    _publication(db, content, platform=Platform.DEVTO, status=PublicationStatus.FAILED)
    _publication(
        db, content, platform=Platform.HASHNODE, status=PublicationStatus.PUBLISHED
    )
    db.commit()

    publishing_service.sync_content_status(content)

    assert content.status == ContentStatus.PUBLISHED
    assert content.published_at is not None


def test_every_platform_terminal_and_none_published_is_a_failed_piece(db, project):
    content = _content(db, project, status=ContentStatus.APPROVED)
    _publication(db, content, platform=Platform.DEVTO, status=PublicationStatus.FAILED)
    _publication(
        db, content, platform=Platform.HASHNODE, status=PublicationStatus.FAILED
    )
    db.commit()

    publishing_service.sync_content_status(content)

    assert content.status == ContentStatus.FAILED


def test_a_published_piece_keeps_the_timestamp_it_already_had(db, project):
    """A second platform succeeding must not move ``published_at`` — the piece
    was published when the *first* one landed, and the analytics series is
    anchored to that date."""
    first = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    content = _content(db, project, status=ContentStatus.PUBLISHED, published_at=first)
    _publication(
        db, content, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    db.commit()

    publishing_service.sync_content_status(content)

    # SQLite hands the column back naive; the instant is what matters here.
    assert content.published_at.replace(tzinfo=UTC) == first


# --------------------------------------------------------------------------- #
# Retry backoff                                                               #
# --------------------------------------------------------------------------- #


def test_a_zero_retry_window_parks_the_row_without_rewriting_the_error(
    db, project, monkeypatch
):
    """``PUBLISH_RETRY_DEFER_SECONDS=0`` turns the backoff off.

    The row still goes to SCHEDULED so the sweep picks it up, but appending
    "retrying in 0s" to the error would be noise pretending to be information.
    """
    monkeypatch.setattr(settings, "publish_retry_defer_seconds", 0)
    content = _content(db, project)
    publication = _publication(
        db, content, status=PublicationStatus.PENDING, attempts=1
    )
    db.commit()

    publishing_service._fail(db, publication, "devto said 503", terminal=False)

    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.error == "devto said 503"


def test_a_nonzero_retry_window_says_when_it_will_come_back(db, project, monkeypatch):
    monkeypatch.setattr(settings, "publish_retry_defer_seconds", 300)
    monkeypatch.setattr(settings, "publish_retry_max_defer_seconds", 3600)
    content = _content(db, project)
    publication = _publication(
        db, content, status=PublicationStatus.PENDING, attempts=1
    )
    db.commit()

    publishing_service._fail(db, publication, "devto said 503", terminal=False)

    assert "retrying in 300s" in publication.error


# --------------------------------------------------------------------------- #
# Metrics collection                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "external_id"),
    [
        pytest.param(PublicationStatus.SCHEDULED, "123", id="not-published"),
        pytest.param(PublicationStatus.PUBLISHED, None, id="no-external-id"),
        pytest.param(PublicationStatus.PUBLISHED, "", id="blank-external-id"),
        pytest.param(PublicationStatus.FAILED, None, id="failed"),
    ],
)
def test_a_publication_with_nothing_to_poll_is_skipped(
    db, project, status, external_id
):
    """No external id means no post to ask about. Without this guard the
    adapter is handed ``None`` as a post id and the platform answers 404 for
    every unpublished row on every sweep."""
    content = _content(db, project)
    publication = _publication(
        db, content, platform=Platform.DEVTO, status=status, external_id=external_id
    )
    db.commit()

    assert publishing_service.collect_metrics(db, publication) is None


def test_a_platform_that_reports_no_metrics_is_not_asked_for_any(db, project):
    """Medium has no read API. Asking anyway costs a request per post per sweep
    and answers 404 every time."""
    content = _content(db, project)
    publication = _publication(
        db,
        content,
        platform=Platform.MEDIUM,
        status=PublicationStatus.PUBLISHED,
        external_id="abc123",
    )
    db.commit()

    assert publishing_service.collect_metrics(db, publication) is None


def test_a_rate_limit_stands_the_account_down_for_the_rest_of_the_sweep(
    db, project, monkeypatch
):
    """The set is the sweep's memory. One 429 has to stop the *next* request on
    that (user, platform), not just this one — otherwise the poller answers a
    rate limit by making one refused request per published post."""
    content = _content(db, project)
    publication = _publication(
        db,
        content,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="abc123",
    )
    db.commit()

    monkeypatch.setattr(
        publishing_service, "_credentials_for", lambda *a, **k: {"api_key": "k"}
    )
    monkeypatch.setattr(
        publishing_service.publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda *a, **k: (_ for _ in ()).throw(RateLimited("429 slow down")),
    )

    seen: set = set()
    assert publishing_service.collect_metrics(db, publication, rate_limited=seen) is None
    assert (project.user_id, Platform.DEVTO) in seen


def test_a_rate_limit_with_no_sweep_memory_still_declines_quietly(
    db, project, monkeypatch
):
    """The single-post refresh button passes no set — each call stands alone,
    and the 429 has to land as ``None`` rather than an exception in the route."""
    content = _content(db, project)
    publication = _publication(
        db,
        content,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="abc123",
    )
    db.commit()

    monkeypatch.setattr(
        publishing_service, "_credentials_for", lambda *a, **k: {"api_key": "k"}
    )
    monkeypatch.setattr(
        publishing_service.publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda *a, **k: (_ for _ in ()).throw(RateLimited("429 slow down")),
    )

    assert publishing_service.collect_metrics(db, publication, rate_limited=None) is None


def test_an_already_refused_account_is_skipped_without_a_request(
    db, project, monkeypatch
):
    content = _content(db, project)
    publication = _publication(
        db,
        content,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="abc123",
    )
    db.commit()

    called: list[str] = []
    monkeypatch.setattr(
        publishing_service.publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda *a, **k: called.append("asked"),
    )

    result = publishing_service.collect_metrics(
        db, publication, rate_limited={(project.user_id, Platform.DEVTO)}
    )

    assert result is None
    assert called == []


def test_a_publish_error_during_a_metrics_poll_is_swallowed(db, project, monkeypatch):
    """Metrics are a nice-to-have. A platform having a bad afternoon must not
    fill the log with errors or mark the connection invalid — the post is still
    live, and the counters catch up on the next sweep."""
    content = _content(db, project)
    publication = _publication(
        db,
        content,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="abc123",
    )
    db.commit()

    monkeypatch.setattr(
        publishing_service, "_credentials_for", lambda *a, **k: {"api_key": "k"}
    )
    monkeypatch.setattr(
        publishing_service.publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda *a, **k: (_ for _ in ()).throw(PublishError("502 from devto")),
    )

    assert publishing_service.collect_metrics(db, publication) is None
