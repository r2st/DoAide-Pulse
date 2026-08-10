"""``validate_config`` — the edge check that keeps a trigger from failing silently.

The point of validating at the schema rather than at poll time is that the
person who typed the number is still looking at the form. So these tests care
about two things: that a bad value is refused *here*, and that a good-but-loosely
typed one (``"6"``, ``6.0``) is coerced to the type the poller will later assume
it has. A ``hour_utc`` that stays the string ``"9"`` compares wrong against
``now.hour`` and the schedule fires never or always.
"""
from __future__ import annotations

import pytest

from app.models.trigger import TriggerKind
from app.schemas.trigger import MAX_INTERVAL_HOURS, TriggerCreate, validate_config

# --------------------------------------------------------------------------- #
# Shape: unknown and missing keys                                              #
# --------------------------------------------------------------------------- #


def test_a_typoed_key_is_named_in_the_error_rather_than_ignored():
    with pytest.raises(ValueError) as exc:
        validate_config(TriggerKind.RSS, {"feed_uri": "https://example.com/f.xml"})

    message = str(exc.value)
    assert "feed_uri" in message
    # ...and the message says what it *would* have accepted.
    assert "feed_url" in message


def test_every_unknown_key_is_reported_not_just_the_first():
    with pytest.raises(ValueError) as exc:
        validate_config(
            TriggerKind.SCHEDULE, {"topic": "A", "hour": 9, "interval": 3}
        )

    message = str(exc.value)
    assert "hour" in message and "interval" in message


def test_a_key_belonging_to_another_kind_is_refused():
    """``repo`` is a github setting; on a schedule it is a trigger that never fires."""
    with pytest.raises(ValueError, match="repo"):
        validate_config(TriggerKind.SCHEDULE, {"repo": "r2st/Herald"})


def test_a_required_key_present_but_blank_counts_as_missing():
    with pytest.raises(ValueError, match="needs feed_url"):
        validate_config(TriggerKind.RSS, {"feed_url": "   "})


def test_an_empty_config_is_fine_for_a_kind_that_requires_nothing():
    assert validate_config(TriggerKind.WEBHOOK, {}) == {}


def test_none_is_accepted_as_an_empty_config():
    assert validate_config(TriggerKind.WEBHOOK, None) == {}


def test_the_returned_config_is_a_copy_not_the_caller_s_dict():
    original = {"topic": "A", "hour_utc": "9"}

    result = validate_config(TriggerKind.SCHEDULE, original)

    assert result["hour_utc"] == 9
    assert original["hour_utc"] == "9", "the caller's dict was mutated"


# --------------------------------------------------------------------------- #
# content_type                                                                 #
# --------------------------------------------------------------------------- #


def test_an_unknown_content_type_is_refused_and_lists_the_real_ones():
    with pytest.raises(ValueError) as exc:
        validate_config(TriggerKind.WEBHOOK, {"content_type": "haiku"})

    message = str(exc.value)
    assert "haiku" in message
    assert "tutorial" in message


def test_a_content_type_is_matched_case_insensitively():
    config = validate_config(TriggerKind.WEBHOOK, {"content_type": "TUTORIAL"})

    assert config["content_type"] == "TUTORIAL"


def test_a_blank_content_type_is_left_alone_rather_than_refused():
    """Falsy means "unset"; only a *wrong* value is an error."""
    assert validate_config(TriggerKind.WEBHOOK, {"content_type": ""}) == {
        "content_type": ""
    }


# --------------------------------------------------------------------------- #
# every_hours                                                                  #
# --------------------------------------------------------------------------- #


def test_every_hours_arrives_as_a_string_and_leaves_as_a_float():
    config = validate_config(TriggerKind.WEBHOOK, {"every_hours": "6"})

    assert config["every_hours"] == 6.0
    assert isinstance(config["every_hours"], float)


def test_every_hours_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match="every_hours must be a number"):
        validate_config(TriggerKind.WEBHOOK, {"every_hours": "soon"})


def test_every_hours_of_a_wrong_type_is_refused():
    with pytest.raises(ValueError, match="every_hours must be a number"):
        validate_config(TriggerKind.WEBHOOK, {"every_hours": ["6"]})


def test_every_hours_of_zero_is_refused():
    """Zero is not "as often as possible", it is a busy loop."""
    with pytest.raises(ValueError, match="between 0 and"):
        validate_config(TriggerKind.WEBHOOK, {"every_hours": 0})


def test_a_negative_every_hours_is_refused():
    with pytest.raises(ValueError, match="between 0 and"):
        validate_config(TriggerKind.WEBHOOK, {"every_hours": -1})


def test_every_hours_beyond_a_year_is_refused():
    with pytest.raises(ValueError, match="one year"):
        validate_config(TriggerKind.WEBHOOK, {"every_hours": MAX_INTERVAL_HOURS + 1})


def test_every_hours_of_exactly_a_year_is_allowed():
    config = validate_config(TriggerKind.WEBHOOK, {"every_hours": MAX_INTERVAL_HOURS})

    assert config["every_hours"] == float(MAX_INTERVAL_HOURS)


def test_a_fractional_every_hours_survives_as_a_fraction():
    config = validate_config(TriggerKind.WEBHOOK, {"every_hours": 0.5})

    assert config["every_hours"] == 0.5


def test_every_hours_set_to_none_is_left_alone():
    """An explicit null means "no override", not "zero hours"."""
    assert validate_config(TriggerKind.WEBHOOK, {"every_hours": None}) == {
        "every_hours": None
    }


# --------------------------------------------------------------------------- #
# hour_utc                                                                     #
# --------------------------------------------------------------------------- #


def test_hour_utc_arrives_as_a_string_and_leaves_as_an_int():
    config = validate_config(TriggerKind.SCHEDULE, {"topic": "A", "hour_utc": "9"})

    assert config["hour_utc"] == 9
    assert isinstance(config["hour_utc"], int)


def test_hour_utc_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match="0-23"):
        validate_config(TriggerKind.SCHEDULE, {"hour_utc": "morning"})


def test_hour_utc_of_a_wrong_type_is_refused():
    with pytest.raises(ValueError, match="0-23"):
        validate_config(TriggerKind.SCHEDULE, {"hour_utc": {"at": 9}})


@pytest.mark.parametrize("hour", [-1, 24, 25])
def test_an_hour_outside_the_day_is_refused(hour):
    with pytest.raises(ValueError, match="0-23"):
        validate_config(TriggerKind.SCHEDULE, {"hour_utc": hour})


@pytest.mark.parametrize("hour", [0, 23])
def test_both_ends_of_the_day_are_allowed(hour):
    """Midnight is a real hour — an exclusive lower bound would lose it."""
    config = validate_config(TriggerKind.SCHEDULE, {"hour_utc": hour})

    assert config["hour_utc"] == hour


def test_hour_utc_set_to_none_is_left_alone():
    assert validate_config(TriggerKind.SCHEDULE, {"hour_utc": None}) == {
        "hour_utc": None
    }


# --------------------------------------------------------------------------- #
# commit_threshold                                                             #
# --------------------------------------------------------------------------- #


def test_commit_threshold_arrives_as_a_string_and_leaves_as_an_int():
    config = validate_config(TriggerKind.GITHUB, {"commit_threshold": "5"})

    assert config["commit_threshold"] == 5
    assert isinstance(config["commit_threshold"], int)


def test_commit_threshold_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match="whole number"):
        validate_config(TriggerKind.GITHUB, {"commit_threshold": "a few"})


def test_commit_threshold_of_a_wrong_type_is_refused():
    with pytest.raises(ValueError, match="whole number"):
        validate_config(TriggerKind.GITHUB, {"commit_threshold": [5]})


@pytest.mark.parametrize("threshold", [0, -3])
def test_a_threshold_below_one_is_refused(threshold):
    """Zero commits is "write about nothing having happened"."""
    with pytest.raises(ValueError, match="at least 1"):
        validate_config(TriggerKind.GITHUB, {"commit_threshold": threshold})


def test_a_threshold_of_one_is_allowed():
    config = validate_config(TriggerKind.GITHUB, {"commit_threshold": 1})

    assert config["commit_threshold"] == 1


def test_commit_threshold_set_to_none_is_left_alone():
    assert validate_config(TriggerKind.GITHUB, {"commit_threshold": None}) == {
        "commit_threshold": None
    }


# --------------------------------------------------------------------------- #
# RSS feed URLs                                                                #
# --------------------------------------------------------------------------- #


def test_a_feed_url_is_stripped_on_the_way_through():
    config = validate_config(
        TriggerKind.RSS, {"feed_url": "  https://example.com/feed.xml  "}
    )

    assert config["feed_url"] == "https://example.com/feed.xml"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed.xml",
        "http://localhost/feed.xml",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "not a url at all",
    ],
)
def test_a_feed_url_herald_would_refuse_to_fetch_is_refused_at_the_form(url):
    with pytest.raises(ValueError):
        validate_config(TriggerKind.RSS, {"feed_url": url})


# --------------------------------------------------------------------------- #
# The model wrapper                                                            #
# --------------------------------------------------------------------------- #


def test_the_create_model_stores_the_coerced_config_not_the_raw_one():
    """The coercion has to survive pydantic, or nothing downstream sees it."""
    payload = TriggerCreate(
        project_id=1,
        kind=TriggerKind.SCHEDULE,
        config={"topic": "A", "hour_utc": "9", "every_hours": "24"},
    )

    assert payload.config["hour_utc"] == 9
    assert payload.config["every_hours"] == 24.0


def test_the_create_model_rejects_a_config_its_kind_cannot_use():
    with pytest.raises(ValueError, match="feed_url"):
        TriggerCreate(project_id=1, kind=TriggerKind.RSS, config={})
