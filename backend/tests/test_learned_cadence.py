"""Learning posting times from results, and refusing to when the data is thin.

The table in :mod:`app.services.cadence` is a prior, and the bar for replacing
it is high on purpose. Half of this file is about *not* learning: five posts
scattered across five hours support no conclusion, and a scheduler whose
suggestion moves every week is worse than one that is merely generic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import cadence, learned_cadence, scheduling


def _publish_at(
    db,
    project,
    *,
    slug: str,
    when: datetime,
    first_day_views: int,
    platform: Platform = Platform.DEVTO,
) -> None:
    """A post published at *when* that took *first_day_views* on day one."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Post {slug}",
        slug=slug,
        status=ContentStatus.PUBLISHED,
        published_at=when,
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        published_at=when,
        external_id=f"ext-{slug}",
    )
    db.add(publication)
    db.flush()
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=when + timedelta(hours=22),
            views=first_day_views,
        )
    )
    db.commit()


def _weeks_ago(weeks: int, *, hour: int, weekday: int = 1) -> datetime:
    """A Tuesday (by default) *weeks* back, at *hour* UTC."""
    anchor = datetime.now(UTC) - timedelta(weeks=weeks)
    shift = (anchor.weekday() - weekday) % 7
    return (anchor - timedelta(days=shift)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


# --------------------------------------------------------------------------- #
# Falling back to the table                                                   #
# --------------------------------------------------------------------------- #


def test_with_no_history_the_table_stands(db, project, user):
    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)

    assert learned.is_learned is False
    assert learned.sample == 0
    assert learned.cadence == cadence.cadence_for(Platform.DEVTO)


def test_below_the_sample_bar_the_table_stands(db, project, user):
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=900,
        )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.is_learned is False
    assert learned.sample == 3
    assert learned.cadence.best_hour_utc == cadence.cadence_for(Platform.DEVTO).best_hour_utc


def test_posts_scattered_across_hours_name_no_winner(db, project, user):
    """Enough posts overall, but no hour with enough of its own."""
    for index, hour in enumerate([3, 7, 11, 16, 21]):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=hour),
            first_day_views=500 + index,
        )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.sample == 5
    assert learned.is_learned is False


def test_posts_too_young_to_have_a_first_day_are_not_observations(db, project, user):
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=datetime.now(UTC) - timedelta(hours=2),
            first_day_views=900,
        )

    # Published two hours ago: no first-day number exists yet for any of them.
    assert learned_cadence.observations(db, user.id, Platform.DEVTO) == []
    assert learned_cadence.learn(db, user.id, Platform.DEVTO).is_learned is False


def test_disabling_the_feature_restores_the_table(db, project, user, monkeypatch):
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=900,
        )
    monkeypatch.setattr(
        learned_cadence.settings, "learned_cadence_enabled", False, raising=False
    )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.is_learned is False
    assert learned.cadence == cadence.cadence_for(Platform.DEVTO)


# --------------------------------------------------------------------------- #
# Learning                                                                    #
# --------------------------------------------------------------------------- #


def test_the_best_hour_is_learned_from_first_day_views(db, project, user):
    """07:00 beats the table's 13:00 when the user's own posts say so."""
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"early-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"late-{index}",
            when=_weeks_ago(index + 6, hour=19),
            first_day_views=100,
        )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.is_learned is True
    assert learned.cadence.best_hour_utc == 7
    assert learned.best_hour_sample == 3
    assert learned.best_hour_median == 2000
    assert "Learned from 6 of your posts" in learned.cadence.rationale


def test_lifetime_views_do_not_decide_it(db, project, user):
    """An old post has had months to accumulate; the comparable is day one."""
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"old-{index}",
            when=_weeks_ago(40 + index, hour=19),
            first_day_views=50,
        )
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"new-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=300,
        )
    # The old posts kept accruing views for months after their first day.
    for publication in db.query(Publication).all():
        if publication.external_id.startswith("ext-old"):
            db.add(
                ContentMetric(
                    publication_id=publication.id,
                    captured_at=datetime.now(UTC) - timedelta(days=1),
                    views=90_000,
                )
            )
    db.commit()

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.cadence.best_hour_utc == 7


def test_max_per_week_is_never_learned(db, project, user):
    """Saturation is a judgement about the audience, not something views say."""
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.is_learned is True
    assert learned.cadence.max_per_week == cadence.cadence_for(Platform.DEVTO).max_per_week


def test_weekdays_narrow_to_the_days_that_worked(db, project, user):
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"tue-{index}",
            when=_weeks_ago(index + 2, hour=7, weekday=1),
            first_day_views=2000,
        )
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"sat-{index}",
            when=_weeks_ago(index + 6, hour=7, weekday=5),
            first_day_views=10,
        )

    learned = learned_cadence.learn(db, user.id, Platform.DEVTO)
    assert learned.cadence.best_weekdays == (1,)


def test_each_platform_learns_separately(db, project, user):
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"devto-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )
    for index in range(3):
        _publish_at(
            db,
            project,
            slug=f"devto-late-{index}",
            when=_weeks_ago(index + 6, hour=19),
            first_day_views=10,
        )
    for index in range(2):
        _publish_at(
            db,
            project,
            slug=f"bsky-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
            platform=Platform.BLUESKY,
        )

    assert learned_cadence.learn(db, user.id, Platform.DEVTO).is_learned is True
    assert learned_cadence.learn(db, user.id, Platform.BLUESKY).is_learned is False


def test_another_users_results_are_not_learned_from(db, project, user):
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )

    assert learned_cadence.learn(db, user.id + 999, Platform.DEVTO).is_learned is False


# --------------------------------------------------------------------------- #
# What the rest of the app does with it                                       #
# --------------------------------------------------------------------------- #


def test_optimal_slots_land_on_the_learned_hour(db, project, user):
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )

    (slot,) = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])
    assert slot.when.hour == 7
    assert "Learned from" in slot.rationale


def test_optimal_slots_use_the_table_without_evidence(db, project, user):
    (slot,) = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])

    assert slot.when.hour == cadence.cadence_for(Platform.DEVTO).best_hour_utc
    assert "Learned from" not in slot.rationale


def test_learned_slots_still_avoid_each_other(db, project, user):
    """Learning a single best hour must not stack a batch on one morning."""
    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )

    slots = cadence.suggest_schedule(
        Platform.DEVTO,
        count=3,
        start=datetime.now(UTC),
        using=learned_cadence.learn(db, user.id, Platform.DEVTO).cadence,
    )
    assert all(slot.hour == 7 for slot in slots)
    assert len(set(slots)) == 3
    gaps = [(b - a).total_seconds() for a, b in zip(slots, slots[1:], strict=False)]
    assert all(gap >= 12 * 3600 for gap in gaps)


def test_cadence_endpoint_says_where_the_answer_came_from(client, auth, db, project, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
        )
    )
    db.commit()

    resp = client.get("/api/v1/calendar/cadence", headers=auth)
    assert resp.status_code == 200
    (entry,) = resp.json()
    assert entry["source"] == "table"
    assert entry["sample"] == 0

    for index in range(6):
        _publish_at(
            db,
            project,
            slug=f"p-{index}",
            when=_weeks_ago(index + 2, hour=7),
            first_day_views=2000,
        )

    (entry,) = client.get("/api/v1/calendar/cadence", headers=auth).json()
    assert entry["source"] == "learned"
    assert entry["best_time_utc"] == "07:00"
    assert entry["sample"] == 6
    assert entry["best_hour_median_views"] == 2000


def test_calendar_cadence_can_be_asked_for_one_platform(client, auth):
    resp = client.get("/api/v1/calendar/cadence?platform=bluesky", headers=auth)

    assert resp.status_code == 200
    (entry,) = resp.json()
    assert entry["platform"] == "bluesky"
    assert entry["source"] == "table"
