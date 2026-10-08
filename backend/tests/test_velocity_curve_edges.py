"""Two things ``velocity.curves`` has to survive: a filter, and a bad timestamp.

The curve is measured in hours since the post went live, and every window in
:mod:`app.services.velocity`, :mod:`app.services.alerts` and the calendar's
cadence learning is a slice of that axis. A snapshot that lands *before* hour
zero would put a negative offset on the axis and corrupt every one of them, and
the timestamps that produce it are ordinary: an import that backdates
``published_at``, or a manual fix to a row.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import velocity


def _now() -> datetime:
    return datetime.now(UTC)


def _published(db, project, *, slug: str, platform=Platform.DEVTO, days_ago: int = 10):
    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title=slug.replace("-", " ").title(),
        slug=slug,
        body_markdown="Body.",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        published_at=_now() - timedelta(days=days_ago),
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return publication


@pytest.fixture
def second_project(db, user):
    row = Project(
        user_id=user.id,
        name="Another thing",
        slug="another-thing",
        description="Also ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_curves_can_be_narrowed_to_one_project(db, user, project, second_project):
    """The dashboard filters by project; the query must, not the caller."""
    _published(db, project, slug="from-pulse")
    _published(db, second_project, slug="from-the-other-thing")

    everything = velocity.curves(db, user.id)
    narrowed = velocity.curves(db, user.id, project_id=second_project.id)

    assert len(everything) == 2
    assert [c.title for c in narrowed] == ["From The Other Thing"]


def test_curves_can_be_narrowed_to_one_platform(db, user, project):
    _published(db, project, slug="on-devto", platform=Platform.DEVTO)
    _published(db, project, slug="on-mastodon", platform=Platform.MASTODON)

    narrowed = velocity.curves(db, user.id, platform=Platform.MASTODON)

    assert [c.platform for c in narrowed] == [Platform.MASTODON]


def test_a_user_with_nothing_published_has_no_curves(db, user):
    assert velocity.curves(db, user.id) == []


def test_a_snapshot_older_than_the_publish_time_is_pinned_to_hour_zero(
    db, user, project
):
    """A backdated ``published_at`` must not produce a negative offset.

    Dropping the reading instead would be worse: it is the earliest thing known
    about the post, and the first-day window is the one every benchmark in
    :mod:`app.services.alerts` is built from.
    """
    publication = _published(db, project, slug="backdated", days_ago=5)
    # Two hours before the recorded publish time.
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=_now() - timedelta(days=5, hours=2),
            views=40,
        )
    )
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=_now() - timedelta(days=4),
            views=100,
        )
    )
    db.commit()

    curve = velocity.curves(db, user.id)[0]

    assert [point.hours for point in curve.points][0] == 0.0
    assert all(point.hours >= 0 for point in curve.points)
    # And the series is still monotonic, which is what makes a subtraction
    # between any two points mean anything.
    assert [point.views for point in curve.points] == [40, 100]
