"""The branches a trigger only reaches when something is wrong with it.

A trigger is one of many, polled by a shared sweep. So almost everything here is
about *not* propagating a failure: a feed that 500s, a repo that was renamed, a
config field holding the wrong type, a generation that blows up. Each has to end
as a recorded reason on a row somebody can look at, and the sweep has to carry
on to the next trigger.

The other half is ``_dig`` and ``_as_text``, the two functions that stand between
an arbitrary inbound JSON body and a prompt. They are handed whatever the sender
felt like sending, so what they do with the shapes nobody designed for is the
whole of their contract.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentType
from app.models.project import AutopilotMode
from app.models.trigger import Trigger, TriggerEventStatus, TriggerKind
from app.services import content_pipeline, feeds, triggers
from app.services.crypto import CredentialEncryptionError
from app.services.signals import TriggerSignal


@pytest.fixture
def writing_project(db, project):
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
# Reading a signing secret that cannot be read                                 #
# --------------------------------------------------------------------------- #


def test_a_trigger_with_no_stored_secret_reads_as_empty(db, project):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)
    trigger.encrypted_secret = None

    assert triggers.read_secret(trigger) == ""


def test_a_secret_encrypted_under_a_rotated_key_reads_as_empty_not_a_crash(
    db, project, monkeypatch, caplog
):
    """Key rotation is a reason to refuse the request, not to 500.

    The user's fix is to rotate the trigger's secret and update the sender; a
    traceback out of the inbound endpoint tells them none of that.
    """
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)
    trigger.encrypted_secret = "gAAAAA-nonsense-from-a-previous-key"
    db.commit()

    def cannot_decrypt(blob):
        raise CredentialEncryptionError("key mismatch")

    monkeypatch.setattr(triggers, "decrypt_credentials", cannot_decrypt)

    with caplog.at_level("WARNING"):
        assert triggers.read_secret(trigger) == ""

    assert "unreadable" in caplog.text


def test_a_secret_blob_that_decrypts_to_nothing_reads_as_empty(
    db, project, monkeypatch
):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)
    trigger.encrypted_secret = "something"
    monkeypatch.setattr(triggers, "decrypt_credentials", lambda blob: {})

    assert triggers.read_secret(trigger) == ""


# --------------------------------------------------------------------------- #
# _dig: following a user-typed path into someone else's JSON                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "path", "expected"),
    [
        ({"a": {"b": "c"}}, "a.b", "c"),
        ({"items": [{"name": "first"}]}, "items.0.name", "first"),
        # A path that runs off the end of a list, rather than raising.
        ({"items": []}, "items.0", None),
        ({"items": [1, 2]}, "items.9", None),
        # A list indexed by something that is not a number.
        ({"items": [1, 2]}, "items.name", None),
        # A path that runs into a scalar and tries to keep going.
        ({"a": "text"}, "a.b", None),
        # A key that simply is not there.
        ({"a": 1}, "b", None),
        # An empty path, and one with an empty segment.
        ({"a": 1}, "", None),
        ({"a": {"b": 1}}, "a..b", None),
        # A null in the middle stops the walk.
        ({"a": None}, "a.b", None),
    ],
)
def test_a_path_that_does_not_fit_the_payload_yields_nothing(payload, path, expected):
    assert triggers._dig(payload, path) == expected


def test_a_negative_index_is_read_as_python_reads_it():
    """Not a designed feature, but pinned: ``-1`` must not become an exception."""
    assert triggers._dig({"items": [1, 2, 3]}, "items.-1") == 3


# --------------------------------------------------------------------------- #
# _as_text: whatever the sender put there, as something a prompt can hold      #
# --------------------------------------------------------------------------- #


def test_a_list_becomes_one_line_per_item_and_is_capped():
    text = triggers._as_text([f"item {i}" for i in range(40)])

    lines = text.splitlines()
    assert lines[0] == "item 0"
    assert len(lines) == 25, "an unbounded list would be an unbounded prompt"


def test_a_dict_becomes_key_value_lines_and_is_capped():
    text = triggers._as_text({f"k{i}": f"v{i}" for i in range(40)})

    assert "k0: v0" in text
    assert len(text.splitlines()) == 25


def test_a_nested_structure_is_flattened_rather_than_dropped():
    text = triggers._as_text({"data": {"title": "Ship it"}})

    assert "Ship it" in text


def test_none_becomes_the_empty_string():
    assert triggers._as_text(None) == ""


@pytest.mark.parametrize("value", [42, 3.5, True])
def test_a_scalar_becomes_its_string_form(value):
    assert triggers._as_text(value) == str(value)


def test_an_object_of_no_known_shape_still_becomes_text():
    class Odd:
        def __str__(self):
            return "odd object"

    assert triggers._as_text(Odd()) == "odd object"


def test_a_very_long_string_is_clipped_to_the_limit():
    assert len(triggers._as_text("x" * 10_000)) <= 10_000
    assert len(triggers._as_text("x" * 10_000, 50)) == 50


# --------------------------------------------------------------------------- #
# content_type_for                                                             #
# --------------------------------------------------------------------------- #


def test_a_config_naming_a_content_type_that_does_not_exist_falls_back_loudly(
    db, project, caplog
):
    """The schema refuses this at the edge, so reaching here means the row
    predates the check or was written directly. Falling back beats crashing the
    sweep, but it is logged so it can be fixed."""
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)
    trigger.config = {"content_type": "haiku"}
    db.commit()

    with caplog.at_level("WARNING"):
        result = triggers.content_type_for(trigger, ContentType.ANNOUNCEMENT)

    assert result == ContentType.ANNOUNCEMENT
    assert "haiku" in caplog.text


def test_an_unset_content_type_takes_the_fallback_without_complaint(
    db, project, caplog
):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    with caplog.at_level("WARNING"):
        result = triggers.content_type_for(trigger, ContentType.HOW_TO)

    assert result == ContentType.HOW_TO
    assert caplog.text == ""


# --------------------------------------------------------------------------- #
# interval_hours and is_due, given values of the wrong type                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["soon", ["6"], {"h": 6}])
def test_an_uninterpretable_interval_falls_back_to_the_default(db, project, bad):
    from app.config import settings

    trigger = _trigger(db, project, TriggerKind.RSS)
    trigger.config = {"every_hours": bad}
    db.commit()

    assert triggers.interval_hours(trigger) == float(
        settings.trigger_default_interval_hours
    )


@pytest.mark.parametrize("bad", [0, -5, None])
def test_a_non_positive_interval_falls_back_to_the_default(db, project, bad):
    from app.config import settings

    trigger = _trigger(db, project, TriggerKind.RSS)
    trigger.config = {"every_hours": bad}
    db.commit()

    assert triggers.interval_hours(trigger) == float(
        settings.trigger_default_interval_hours
    )


def test_an_uninterpretable_hour_utc_is_ignored_rather_than_pinning_the_schedule(
    db, project
):
    """A junk ``hour_utc`` must not mean "never due" — that is a silent trigger."""
    trigger = _trigger(db, project, TriggerKind.SCHEDULE, every_hours=1)
    trigger.config = {"every_hours": 1, "hour_utc": "morning"}
    trigger.last_fired_at = None
    db.commit()

    assert triggers.is_due(trigger) is True


# --------------------------------------------------------------------------- #
# check(): every failure ends as a reason on the row                           #
# --------------------------------------------------------------------------- #


def test_a_webhook_trigger_is_never_polled(db, project):
    trigger = _trigger(db, project, TriggerKind.WEBHOOK)

    assert triggers.check(db, trigger) == {
        "trigger_id": trigger.id,
        "status": "not_polled",
    }
    # ...and the poll it did not do leaves no watermark.
    assert trigger.last_checked_at is None


def test_an_rss_trigger_with_no_feed_url_reports_why(db, project):
    trigger = _trigger(db, project, TriggerKind.RSS)
    trigger.config = {}
    db.commit()

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "no feed URL" in result["error"]
    assert trigger.last_error == "This trigger has no feed URL."
    assert trigger.consecutive_failures == 1


def test_a_feed_that_will_not_load_is_recorded_not_raised(db, project, monkeypatch):
    trigger = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")

    def unreachable(url):
        raise feeds.FeedError("Could not read that feed: connection reset")

    monkeypatch.setattr(feeds, "fetch", unreachable)

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "connection reset" in result["error"]
    assert trigger.last_checked_at is not None


def test_an_unexpected_crash_is_caught_and_recorded_like_any_other_failure(
    db, project, monkeypatch, caplog
):
    """The sweep polls many triggers; one raising must not end the run."""
    trigger = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")

    def explode(url):
        raise ZeroDivisionError("something nobody predicted")

    monkeypatch.setattr(feeds, "fetch", explode)

    with caplog.at_level("ERROR"):
        result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "something nobody predicted" in result["error"]
    assert "Unexpected error" in (trigger.last_error or "")
    assert "crashed" in caplog.text


def test_repeated_failures_eventually_switch_the_trigger_off(
    db, project, monkeypatch
):
    from app.config import settings

    trigger = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")
    monkeypatch.setattr(
        feeds, "fetch", lambda url: (_ for _ in ()).throw(feeds.FeedError("down"))
    )

    for _ in range(settings.trigger_disable_after_failures):
        triggers.check(db, trigger)

    assert trigger.is_active is False


def test_one_success_clears_the_failure_streak(db, project, monkeypatch):
    trigger = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")
    trigger.consecutive_failures = 2
    trigger.last_error = "down"
    db.commit()

    monkeypatch.setattr(
        feeds,
        "fetch",
        lambda url: feeds.Feed(title="Changelog", entries=[]),
    )

    triggers.check(db, trigger)

    assert trigger.consecutive_failures == 0
    assert trigger.last_error is None


# --------------------------------------------------------------------------- #
# GitHub triggers                                                              #
# --------------------------------------------------------------------------- #


def test_a_github_trigger_with_no_repo_anywhere_reports_why(db, project):
    project.repo_url = None
    trigger = _trigger(db, project, TriggerKind.GITHUB)
    trigger.config = {}
    db.commit()

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "no repository" in result["error"]


def test_a_github_trigger_with_no_repo_of_its_own_borrows_the_project_s(
    db, project, monkeypatch
):
    """Configuring the repo twice is the kind of thing users get wrong."""
    from app.services import github_client

    trigger = _trigger(db, project, TriggerKind.GITHUB)
    trigger.config = {}
    db.commit()

    asked: list[str] = []

    def fetch(repo, **kwargs):
        asked.append(repo)
        return github_client.RepoActivity(full_name=repo, head_sha="abc")

    monkeypatch.setattr(github_client, "fetch_activity", fetch)

    triggers.check(db, trigger)

    assert asked == ["r2st/Herald"]


def test_a_github_rate_limit_leaves_the_watermark_alone(db, project, monkeypatch):
    """We genuinely did not look, so the next poll must look at the same range."""
    from app.services import github_client

    trigger = _trigger(db, project, TriggerKind.GITHUB, repo="r2st/Herald")
    trigger.state = {"last_sha": "abc123", "last_tag": "v1.0"}
    db.commit()

    monkeypatch.setattr(
        github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(
            github_client.GitHubRateLimited("rate limit exhausted")
        ),
    )

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "rate limit exhausted" in result["error"]
    db.refresh(trigger)
    assert trigger.state["last_sha"] == "abc123"


def test_any_other_github_failure_is_recorded_the_same_way(db, project, monkeypatch):
    from app.services import github_client

    trigger = _trigger(db, project, TriggerKind.GITHUB, repo="r2st/Herald")
    monkeypatch.setattr(
        github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(github_client.GitHubError("repo gone")),
    )

    result = triggers.check(db, trigger)

    assert result["status"] == "error"
    assert "repo gone" in result["error"]


def test_a_quiet_repo_after_the_baseline_scan_reports_no_news(
    db, project, monkeypatch
):
    from app.services import github_client

    trigger = _trigger(db, project, TriggerKind.GITHUB, repo="r2st/Herald")
    trigger.state = {"last_sha": "abc123"}
    db.commit()

    monkeypatch.setattr(
        github_client,
        "fetch_activity",
        lambda *a, **k: github_client.RepoActivity(
            full_name="r2st/Herald", head_sha="abc123"
        ),
    )

    assert triggers.check(db, trigger)["status"] == "no_news"


# --------------------------------------------------------------------------- #
# Schedules and empty feeds                                                    #
# --------------------------------------------------------------------------- #


def test_a_schedule_checked_before_its_window_is_not_due(db, project):
    from app.models.mixins import utcnow

    trigger = _trigger(db, project, TriggerKind.SCHEDULE, every_hours=168)
    trigger.last_fired_at = utcnow()
    db.commit()

    assert triggers.check(db, trigger)["status"] == "not_due"


def test_a_poll_that_finds_no_new_entries_reports_no_news(db, project, monkeypatch):
    """Baselined already, and nothing has appeared since."""
    trigger = _trigger(db, project, TriggerKind.RSS, feed_url="https://example.com/f.xml")
    trigger.state = {"seen_ids": ["e1"]}
    db.commit()

    monkeypatch.setattr(
        feeds,
        "fetch",
        lambda url: feeds.Feed(
            title="Changelog",
            entries=[feeds.FeedEntry(entry_id="e1", title="Old", summary="", link="")],
        ),
    )

    assert triggers.check(db, trigger)["status"] == "no_news"


def test_no_entries_produces_no_signal_at_all():
    trigger = Trigger(project_id=1, kind=TriggerKind.RSS, name="t", config={})

    assert triggers.signal_from_entries(trigger, feeds.Feed(title="t", entries=[]), []) is None


# --------------------------------------------------------------------------- #
# fire(): a generation that fails is a reason on the event, not an exception   #
# --------------------------------------------------------------------------- #


def _signal() -> TriggerSignal:
    return TriggerSignal(
        kind=TriggerKind.WEBHOOK,
        source="test",
        headline="Something happened",
        summary="Details about the thing.",
        dedupe_key="k1",
        suggested_type=ContentType.ANNOUNCEMENT,
    )


def test_a_generation_that_blows_up_lands_on_the_event_row(
    db, writing_project, monkeypatch, caplog
):
    """The user looks at the event row for this, so that is where it has to be."""
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)

    def explode(*args, **kwargs):
        raise RuntimeError("the model returned something unusable")

    monkeypatch.setattr(content_pipeline, "generate_and_route", explode)

    with caplog.at_level("ERROR"):
        event = triggers.fire(db, trigger, _signal())

    assert event is not None
    assert event.status == TriggerEventStatus.FAILED
    assert "the model returned something unusable" in event.detail
    # The firing still counted — it happened, it just did not produce a piece.
    db.refresh(trigger)
    assert trigger.fire_count == 1


def test_a_failed_generation_leaves_no_half_written_content_behind(
    db, writing_project, monkeypatch
):
    """The rollback matters: a partially flushed piece would show up in the list."""
    from app.models.content import Content

    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)

    def explode_after_adding(session, *args, **kwargs):
        session.add(
            Content(
                project_id=writing_project.id,
                content_type=ContentType.ANNOUNCEMENT,
                title="Half written",
                slug="half-written",
                body_markdown="",
            )
        )
        session.flush()
        raise RuntimeError("failed after writing")

    monkeypatch.setattr(content_pipeline, "generate_and_route", explode_after_adding)

    triggers.fire(db, trigger, _signal())

    assert db.query(Content).filter_by(slug="half-written").count() == 0


def test_a_signal_carrying_no_news_is_skipped_before_the_model_is_called(
    db, writing_project, monkeypatch
):
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)

    def fail(*args, **kwargs):  # pragma: no cover - asserted by not being called
        raise AssertionError("an empty signal must not reach the model")

    monkeypatch.setattr(content_pipeline, "generate_and_route", fail)

    event = triggers.fire(
        db,
        trigger,
        TriggerSignal(
            kind=TriggerKind.WEBHOOK, source="test", headline="", summary=""
        ),
    )

    assert event is not None
    assert event.status == TriggerEventStatus.SKIPPED
    assert "nothing to write about" in event.detail


# --------------------------------------------------------------------------- #
# A failure that arrives as a failed *commit*                                  #
# --------------------------------------------------------------------------- #
#
# The two handlers below are written to turn a crash into a recorded reason on a
# row. Both of them then have to write that reason — and if the crash was a
# commit failing, the session refuses to emit SQL until somebody rolls back. So
# the recovery path was itself unreachable in exactly the case where the session
# is dirty, and what came out instead was a ``PendingRollbackError`` from inside
# the ``except`` arm.
#
# ``tests/test_sweep_survives_a_failed_write.py`` covers the same rule for the
# periodic sweeps.


def _poison(session) -> None:
    """Leave *session* as a failed commit leaves it: dirty, refusing SQL."""
    from app.models.content import Content

    session.add(Content(project_id=None, title="x", slug="x", body_markdown=""))
    session.commit()


def test_a_generation_whose_commit_fails_still_lands_on_the_event_row(
    db, writing_project, monkeypatch, caplog
):
    """A dirty session must not cost the user the only record of the failure.

    ``generate_and_route`` commits, so "it blew up" and "its commit blew up" are
    both ordinary outcomes. The second one used to escape the handler on the log
    line — ``trigger.id`` needs a SELECT the session will not run — leaving the
    event ``pending`` forever with nothing to say why.
    """
    trigger = _trigger(db, writing_project, TriggerKind.WEBHOOK)

    def explode_on_commit(session, *args, **kwargs):
        _poison(session)

    monkeypatch.setattr(content_pipeline, "generate_and_route", explode_on_commit)

    with caplog.at_level("ERROR"):
        event = triggers.fire(db, trigger, _signal())

    assert event is not None
    assert event.status == TriggerEventStatus.FAILED
    assert "Generation failed" in event.detail


def test_a_poll_whose_commit_fails_still_records_the_error_on_the_trigger(
    db, project, monkeypatch, caplog
):
    """``_mark_checked`` is the only thing that counts a failure. It has to run.

    An RSS or GitHub poll that finds news writes an event and commits, so the
    handler in ``check`` can be entered with a dirty session. Its own commit then
    raised, so the error never reached the row — and a trigger failing this way
    could never accumulate the ``consecutive_failures`` that deactivate it.
    """
    trigger = _trigger(
        db, project, TriggerKind.RSS, feed_url="https://example.test/feed.xml"
    )

    def explode_on_commit(session, _trigger_row):
        _poison(session)

    monkeypatch.setattr(triggers, "_check_rss", explode_on_commit)

    with caplog.at_level("ERROR"):
        result = triggers.check(db, trigger)

    assert result["status"] == "error"
    db.refresh(trigger)
    assert trigger.consecutive_failures == 1
    assert "Unexpected error" in trigger.last_error
    assert trigger.last_checked_at is not None
