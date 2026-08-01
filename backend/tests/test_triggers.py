"""The generalized trigger system: four kinds, one pipeline, one dedupe rule.

No LLM provider is reachable in tests, so every firing that reaches generation
takes the template fallback — ``confidence=0.0``, which can never clear the
auto-publish gate. That is what makes these assertions about *routing* rather
than about prose: a fired trigger produces a piece in the review queue, and the
tests check that it produced exactly one.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentType
from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.services import feeds, signals, triggers
from app.services.signals import TriggerSignal

FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Changelog</title>
  <item><title>Webhook triggers shipped</title><guid>e2</guid>
        <link>https://example.com/2</link><description>The new thing.</description></item>
  <item><title>RSS triggers shipped</title><guid>e1</guid>
        <link>https://example.com/1</link><description>The older thing.</description></item>
</channel></rss>
"""


@pytest.fixture
def writing_project(db, project):
    """A project whose autopilot is on, so a trigger actually writes."""
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    return project


def _trigger(db, project, kind, **config) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=kind,
        name=f"{kind.value} trigger",
        config=config,
        state={},
    )
    if kind == TriggerKind.WEBHOOK:
        row.token = triggers.generate_token()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Signals                                                                      #
# --------------------------------------------------------------------------- #


def test_a_signal_digest_leads_with_the_headline_and_caps_the_item_list():
    signal = TriggerSignal(
        kind=TriggerKind.RSS,
        source="RSS Changelog",
        headline="Two things shipped",
        summary="The details.",
        items=tuple(f"item {i}" for i in range(50)),
        url="https://example.com",
    )

    digest = signal.digest(max_items=5)

    assert digest.splitlines()[0] == "Two things shipped"
    assert "50 new item(s) (showing the 5 most recent)" in digest
    assert "item 4" in digest
    assert "item 5" not in digest
    assert digest.endswith("Source: https://example.com")


def test_an_empty_signal_produces_no_digest():
    signal = TriggerSignal(kind=TriggerKind.SCHEDULE, source="s", headline="")

    assert signal.has_news is False
    assert signal.digest() == ""


def test_repo_activity_becomes_a_signal_that_keeps_the_editorial_judgement():
    from app.services.github_client import Commit, Release, RepoActivity

    release = RepoActivity(
        full_name="r2st/Herald",
        new_release=Release(
            tag="v1.2.0", name="Triggers", body="Notes.",
            published_at=None, url="https://example.com/r", prerelease=False,
        ),
    )
    commits = RepoActivity(
        full_name="r2st/Herald",
        new_commits=[Commit("abc123", "feat: triggers", "dev", None, "https://x/1")],
    )

    assert signals.from_repo_activity(release).suggested_type is ContentType.ANNOUNCEMENT
    assert (
        signals.from_repo_activity(commits).suggested_type
        is ContentType.FEATURE_SPOTLIGHT
    )
    # The dedupe key follows the news, so rescanning the same head is a no-op.
    assert signals.from_repo_activity(commits).dedupe_key == (
        signals.from_repo_activity(commits).dedupe_key
    )


# --------------------------------------------------------------------------- #
# Webhook triggers                                                             #
# --------------------------------------------------------------------------- #


def test_a_webhook_body_maps_through_the_configured_paths(db, project):
    trigger = _trigger(
        db,
        project,
        TriggerKind.WEBHOOK,
        headline_path="data.title",
        summary_path="data.body",
        url_path="data.url",
        dedupe_path="data.id",
        content_type="announcement",
    )

    signal = triggers.signal_from_webhook(
        trigger,
        {"data": {"title": "Cycle closed", "body": "12 issues", "url": "https://x/1", "id": "c-9"}},
    )

    assert signal.headline == "Cycle closed"
    assert signal.summary == "12 issues"
    assert signal.url == "https://x/1"
    assert signal.suggested_type is ContentType.ANNOUNCEMENT
    assert signal.dedupe_key is not None


def test_a_webhook_with_no_field_mapping_still_produces_something_writable(db, project):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    signal = triggers.signal_from_webhook(trigger, {"status": "resolved", "region": "eu"})

    assert signal.has_news
    assert "resolved" in signal.summary
    # Nothing identified the event, so nothing is deduplicated on it.
    assert signal.dedupe_key is None


def test_a_missing_path_does_not_break_the_mapping(db, project):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK, headline_path="a.b.c.d")

    signal = triggers.signal_from_webhook(trigger, {"a": {"b": None}})

    assert signal.headline.endswith("fired")


def test_a_list_index_works_in_a_path(db, project):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK, headline_path="items.0.name")

    signal = triggers.signal_from_webhook(trigger, {"items": [{"name": "First"}]})

    assert signal.headline == "First"


# --------------------------------------------------------------------------- #
# Firing and dedupe                                                            #
# --------------------------------------------------------------------------- #


def test_firing_a_trigger_writes_one_piece_and_records_the_event(db, writing_project):
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)
    signal = triggers.signal_from_webhook(trigger, {"title": "Something happened"})

    event = triggers.fire(db, trigger, signal)

    assert event is not None
    assert event.status is TriggerEventStatus.GENERATED
    assert event.content_id is not None
    content = db.get(Content, event.content_id)
    assert content.project_id == writing_project.id
    assert content.source["kind"] == "trigger"
    assert content.source["trigger_id"] == trigger.id
    # The template fallback can never publish itself.
    assert content.confidence == 0.0


def test_the_same_signal_twice_writes_once(db, writing_project):
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK, dedupe_path="id")
    body = {"id": "evt-1", "title": "Deployed"}

    first = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, body))
    second = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, body))

    assert first is not None
    assert second is None
    assert db.query(Content).count() == 1


def test_a_paused_project_logs_the_firing_without_writing(db, project):
    project.is_active = False
    db.commit()
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    event = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, {"a": 1}))

    assert event.status is TriggerEventStatus.SKIPPED
    assert "paused" in event.detail
    assert db.query(Content).count() == 0


def test_autopilot_off_logs_the_firing_without_writing(db, project):
    """The project switch still governs: a trigger is a reason, not permission."""
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    event = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, {"a": 1}))

    assert event.status is TriggerEventStatus.SKIPPED
    assert "autopilot is off" in event.detail


def test_the_daily_limit_stops_a_runaway_source(db, writing_project, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "trigger_daily_content_limit", 1)
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)

    first = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, {"n": 1}))
    second = triggers.fire(db, trigger, triggers.signal_from_webhook(trigger, {"n": 2}))

    assert first.status is TriggerEventStatus.GENERATED
    assert second.status is TriggerEventStatus.SKIPPED
    assert "Daily limit" in second.detail


# --------------------------------------------------------------------------- #
# RSS triggers                                                                 #
# --------------------------------------------------------------------------- #


def test_the_first_rss_check_baselines_and_writes_nothing(db, writing_project, monkeypatch):
    monkeypatch.setattr(feeds, "fetch", lambda url: feeds.parse(FEED_XML))
    trigger = _trigger(
        db, writing_project, TriggerKind.RSS, feed_url="https://example.com/feed.xml"
    )

    result = triggers.check(db, trigger)

    assert result["status"] == "baselined"
    assert db.query(Content).count() == 0
    assert set(trigger.state["seen_ids"]) == {"e1", "e2"}


def test_a_new_rss_entry_writes_one_piece_covering_all_of_them(
    db, writing_project, monkeypatch
):
    monkeypatch.setattr(feeds, "fetch", lambda url: feeds.parse(FEED_XML))
    trigger = _trigger(
        db, writing_project, TriggerKind.RSS, feed_url="https://example.com/feed.xml"
    )
    # Already seen the older entry; the newer one is news.
    trigger.state = {"seen_ids": ["e1"]}
    db.commit()

    result = triggers.check(db, trigger)

    assert result["status"] == TriggerEventStatus.GENERATED.value
    assert db.query(Content).count() == 1
    event = db.get(TriggerEvent, result["event_id"])
    assert event.headline == "Webhook triggers shipped"


def test_an_unreachable_feed_records_the_error_without_crashing(
    db, writing_project, monkeypatch
):
    def boom(url):
        raise feeds.FeedError("The feed returned 500.")

    monkeypatch.setattr(feeds, "fetch", boom)
    trigger = _trigger(
        db, writing_project, TriggerKind.RSS, feed_url="https://example.com/feed.xml"
    )

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert trigger.consecutive_failures == 1
    assert "500" in trigger.last_error


def test_a_feed_that_keeps_failing_is_deactivated(db, writing_project, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "trigger_disable_after_failures", 2)
    monkeypatch.setattr(
        feeds, "fetch", lambda url: (_ for _ in ()).throw(feeds.FeedError("gone"))
    )
    trigger = _trigger(
        db, writing_project, TriggerKind.RSS, feed_url="https://example.com/feed.xml"
    )

    triggers.check(db, trigger)
    triggers.check(db, trigger)

    assert trigger.is_active is False


def test_one_poll_acts_on_at_most_the_configured_number_of_entries(
    db, writing_project, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "feed_max_new_entries", 1)
    monkeypatch.setattr(feeds, "fetch", lambda url: feeds.parse(FEED_XML))
    trigger = _trigger(
        db, writing_project, TriggerKind.RSS, feed_url="https://example.com/feed.xml"
    )
    trigger.state = {"seen_ids": []}
    db.commit()

    triggers.check(db, trigger)

    # Both entries are recorded as seen even though only one was written about,
    # so the backlog is not replayed on the next poll.
    assert set(trigger.state["seen_ids"]) == {"e1", "e2"}
    assert db.query(Content).count() == 1


# --------------------------------------------------------------------------- #
# Schedule triggers                                                            #
# --------------------------------------------------------------------------- #


def test_a_schedule_trigger_is_due_when_it_has_never_fired(db, project):
    trigger = _trigger(db, project, TriggerKind.SCHEDULE, every_hours=168)

    assert triggers.is_due(trigger) is True


def test_a_schedule_trigger_is_not_due_inside_its_window(db, project):
    trigger = _trigger(db, project, TriggerKind.SCHEDULE, every_hours=168)
    trigger.last_fired_at = utcnow()
    db.commit()

    assert triggers.is_due(trigger) is False


def test_an_hour_of_day_gate_holds_a_schedule_back(db, project):
    now = utcnow()
    trigger = _trigger(
        db, project, TriggerKind.SCHEDULE, every_hours=24, hour_utc=(now.hour + 1) % 24
    )

    assert triggers.is_due(trigger) is False


def test_a_schedule_firing_writes_and_will_not_fire_twice_in_the_same_hour(
    db, writing_project
):
    trigger = _trigger(
        db,
        writing_project,
        TriggerKind.SCHEDULE,
        every_hours=1,
        topic="The weekly roundup",
        content_type="how_to",
    )

    first = triggers.check(db, trigger)
    # Force it due again inside the same clock hour; the dedupe key is the slot.
    trigger.last_fired_at = None
    db.commit()
    second = triggers.check(db, trigger)

    assert first["status"] == TriggerEventStatus.GENERATED.value
    assert second["status"] == "duplicate"
    assert db.query(Content).count() == 1
    assert db.query(Content).one().content_type is ContentType.HOW_TO


# --------------------------------------------------------------------------- #
# Due-ness sweep                                                               #
# --------------------------------------------------------------------------- #


def test_the_sweep_ignores_inbound_webhooks_and_inactive_triggers(db, project):
    _trigger(db, project, TriggerKind.WEBHOOK)
    rss = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")
    paused = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/g.xml")
    paused.is_active = False
    db.commit()

    due = triggers.due_triggers(db)

    assert [t.id for t in due] == [rss.id]


def test_the_sweep_skips_triggers_on_a_paused_project(db, project):
    _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")
    project.is_active = False
    db.commit()

    assert triggers.due_triggers(db) == []
