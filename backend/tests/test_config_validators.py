"""The settings that refuse to load rather than misbehave quietly.

Each of these guards a number that has a meaning outside its own range: a
weekday that is not a weekday, an hour that is not an hour, a window of zero
days, a negative retry count. A bad value in ``.env`` fails at import, where the
operator is looking, instead of at 08:00 on a Sunday that never comes.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**over) -> Settings:
    return Settings(jwt_secret="x" * 40, **over)


@pytest.mark.parametrize("value", [-1, 7, 99])
def test_a_digest_weekday_outside_the_week_is_refused(value):
    with pytest.raises(ValidationError, match="0 \\(Monday\\) through 6 \\(Sunday\\)"):
        _settings(digest_send_weekday=value)


@pytest.mark.parametrize("value", [-1, 24])
def test_a_digest_hour_outside_the_day_is_refused(value):
    with pytest.raises(ValidationError, match="hour of the day, 0-23"):
        _settings(digest_send_hour=value)


@pytest.mark.parametrize(
    "field", ["digest_window_days", "schedule_max_horizon_days"]
)
def test_a_window_of_no_days_is_refused(field):
    """Zero would make the digest cover nothing and the horizon reject everything."""
    with pytest.raises(ValidationError, match="at least 1 day"):
        _settings(**{field: 0})


@pytest.mark.parametrize(
    "field",
    [
        "publish_request_retries",
        "publish_rate_limit_max_defer_seconds",
        "schedule_past_grace_seconds",
    ],
)
def test_a_negative_count_or_delay_is_refused(field):
    with pytest.raises(ValidationError, match="zero or positive"):
        _settings(**{field: -1})


@pytest.mark.parametrize(
    "field", ["publish_retry_defer_seconds", "publish_retry_max_defer_seconds"]
)
def test_a_negative_backoff_is_refused_but_zero_is_allowed(field):
    """Zero means "no wait between attempts", which is how this once behaved."""
    with pytest.raises(ValidationError, match="zero or positive"):
        _settings(**{field: -0.5})

    assert getattr(_settings(**{field: 0.0}), field) == 0.0


def test_the_edges_of_each_range_are_accepted():
    ok = _settings(
        digest_send_weekday=6,
        digest_send_hour=23,
        digest_window_days=1,
        schedule_max_horizon_days=1,
        publish_request_retries=0,
        schedule_past_grace_seconds=0,
    )
    assert ok.digest_send_weekday == 6
    assert ok.digest_send_hour == 23
    assert ok.digest_window_days == 1
