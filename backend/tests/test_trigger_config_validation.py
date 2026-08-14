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
from app.schemas.trigger import (
    ALLOWED_CONFIG,
    MAX_CONFIG_LENGTHS,
    MAX_INTERVAL_HOURS,
    TriggerCreate,
    validate_config,
)

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
# Free-text bounds                                                             #
# --------------------------------------------------------------------------- #

#: A kind that accepts each capped key, so the length check is what refuses the
#: value rather than the unknown-key check that runs before it.
_KIND_FOR_KEY = {
    "instructions": TriggerKind.WEBHOOK,
    "topic": TriggerKind.SCHEDULE,
    "headline_path": TriggerKind.WEBHOOK,
    "summary_path": TriggerKind.WEBHOOK,
    "url_path": TriggerKind.WEBHOOK,
    "dedupe_path": TriggerKind.WEBHOOK,
}


@pytest.mark.parametrize("key,cap", sorted(MAX_CONFIG_LENGTHS.items()))
def test_a_free_text_setting_one_character_over_its_cap_is_refused(key, cap):
    with pytest.raises(ValueError, match=f"{key} must be {cap} characters"):
        validate_config(_KIND_FOR_KEY[key], {key: "x" * (cap + 1)})


@pytest.mark.parametrize("key,cap", sorted(MAX_CONFIG_LENGTHS.items()))
def test_a_free_text_setting_exactly_at_its_cap_is_allowed(key, cap):
    """An off-by-one here refuses a value the UI's own counter called legal."""
    config = validate_config(_KIND_FOR_KEY[key], {key: "x" * cap})

    assert config[key] == "x" * cap


def test_every_capped_key_is_one_some_kind_actually_accepts():
    """A cap on a key no kind allows is dead code that reads as protection."""
    accepted = {key for keys in ALLOWED_CONFIG.values() for key in keys}

    assert set(MAX_CONFIG_LENGTHS) <= accepted


def test_no_kind_accepts_an_unbounded_free_text_setting():
    """The gap this closes, kept closed.

    ``config`` is a JSON column, so a new string setting added to
    ``ALLOWED_CONFIG`` is accepted at any length unless something here says
    otherwise — and ``instructions`` reached the model prompt that way. Each key
    below is either capped or checked by name; a fifth kind of setting arriving
    with neither should fail this, not ship.
    """
    checked_another_way = {
        "content_type",  # matched against the ContentType enum
        "every_hours",  # coerced to a bounded float
        "hour_utc",  # coerced to an hour of the day
        "commit_threshold",  # coerced to a positive int
        "require_signature",  # a flag, read as a bool
        "feed_url",  # validate_feed_url, which bounds and parses it
        "repo",  # matched against the owner/name shape
    }
    accepted = {key for keys in ALLOWED_CONFIG.values() for key in keys}

    assert accepted - set(MAX_CONFIG_LENGTHS) - checked_another_way == set()


def test_a_length_is_measured_on_a_non_string_the_same_way():
    """A list is not a way to smuggle a long value past a string-only check."""
    with pytest.raises(ValueError, match="instructions must be"):
        validate_config(TriggerKind.WEBHOOK, {"instructions": ["x"] * 2000})


def test_a_capped_key_set_to_none_is_left_alone():
    assert validate_config(TriggerKind.SCHEDULE, {"topic": None}) == {"topic": None}


def test_the_instructions_cap_matches_the_one_on_the_generate_form():
    """The same text, typed in two places, may not have two different limits."""
    from app.schemas.content import GenerateRequest

    field = GenerateRequest.model_fields["instructions"]
    form_cap = max(
        meta.max_length for meta in field.metadata if getattr(meta, "max_length", None)
    )

    assert MAX_CONFIG_LENGTHS["instructions"] == form_cap


def test_the_topic_cap_matches_the_column_the_headline_lands_in():
    """A topic longer than this was silently truncated by ``record()``."""
    from app.models.trigger import TriggerEvent

    assert (
        MAX_CONFIG_LENGTHS["topic"]
        == TriggerEvent.__table__.c.headline.type.length
    )


# --------------------------------------------------------------------------- #
# repo                                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "repo",
    [
        "r2st/Herald",
        "a/b",
        "some-org/my_repo.js",
        "torvalds/linux",
        "octo-org/.github",  # a leading dot is a real repository name
        "x" * 39 + "/" + "y" * 100,  # both parts at GitHub's own maximum
    ],
)
def test_a_real_repository_name_is_accepted(repo):
    assert validate_config(TriggerKind.GITHUB, {"repo": repo})["repo"] == repo


@pytest.mark.parametrize(
    "repo",
    [
        "just-a-name",  # no owner
        "owner/name/extra",  # reaches a different endpoint
        "owner/..",  # climbs out of /repos/
        "owner/.",
        "../../users/someone",
        "owner/name?per_page=1",  # appends to a query string that exists
        "owner/name#frag",
        "owner/na me",
        "owner/name/",
        "/name",
        "owner/",
        "https://github.com/r2st/Herald",  # the URL, not the full name
        "-owner/name",  # GitHub logins may not start with a hyphen
        "own--er/name",  # nor contain a double one
        "owner-/name",
        "x" * 40 + "/name",  # one over the login maximum
        "owner/" + "y" * 101,  # one over the repository-name maximum
    ],
)
def test_a_repo_that_is_not_owner_slash_name_is_refused(repo):
    with pytest.raises(ValueError, match="not a GitHub repository"):
        validate_config(TriggerKind.GITHUB, {"repo": repo})


def test_the_refusal_quotes_the_value_and_shows_the_shape_wanted():
    with pytest.raises(ValueError) as exc:
        validate_config(TriggerKind.GITHUB, {"repo": "owner/name/extra"})

    message = str(exc.value)
    assert "owner/name/extra" in message
    assert "owner/name" in message


def test_a_blank_repo_is_allowed_because_the_project_supplies_one():
    """``poll_github`` falls back to ``project.repo_full_name`` when unset."""
    assert validate_config(TriggerKind.GITHUB, {"repo": "   "})["repo"] == ""


def test_a_repo_is_stripped_on_the_way_through():
    config = validate_config(TriggerKind.GITHUB, {"repo": "  r2st/Herald\n"})

    assert config["repo"] == "r2st/Herald"


def test_repo_set_to_none_is_left_alone():
    assert validate_config(TriggerKind.GITHUB, {"repo": None}) == {"repo": None}


def test_a_non_string_repo_is_refused_rather_than_stringified_into_a_path():
    with pytest.raises(ValueError, match="not a GitHub repository"):
        validate_config(TriggerKind.GITHUB, {"repo": {"full_name": "r2st/Herald"}})


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
