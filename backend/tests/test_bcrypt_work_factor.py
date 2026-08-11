"""The bcrypt work factor is settable, and production may not lower it.

The setting exists for the test suite. A hash at the default cost of 12 takes
~230ms, the fixtures make a user for very nearly every test, and that one line
accounted for more than half the suite's wall time — enough that a full run
looks like a hang and gets killed rather than waited out. At cost 4 the same
code path costs about 1ms.

That is a fine trade for a suite and a disastrous one for a live password
database, so the guard here is the interesting part: production is held to 12
whatever its .env says. The escape hatch must not be able to follow a copied
.env onto a real box, which is exactly how ``DEBUG=true`` got to production in
the first place.
"""
from __future__ import annotations

import bcrypt
import pytest
from pydantic import ValidationError

from app.config import (
    _BCRYPT_MAX_ROUNDS,
    _BCRYPT_MIN_ROUNDS,
    _BCRYPT_PRODUCTION_MIN_ROUNDS,
    Settings,
)
from app.security import hash_password, verify_password


def _settings(**kwargs):
    return Settings(database_url="sqlite://", _env_file=None, **kwargs)


def _cost_of(hashed: str) -> int:
    """The work factor bcrypt recorded in the hash itself."""
    return int(hashed.split("$")[2])


@pytest.mark.parametrize("rounds", [4, 8, 11])
def test_production_refuses_a_work_factor_below_the_floor(rounds):
    with pytest.raises(ValidationError, match="BCRYPT_ROUNDS"):
        _settings(environment="production", bcrypt_rounds=rounds)


def test_the_refusal_says_why_rather_than_only_what():
    """An error that stops a deploy has to explain itself."""
    with pytest.raises(ValidationError) as exc:
        _settings(environment="production", bcrypt_rounds=4)
    message = str(exc.value)
    assert str(_BCRYPT_PRODUCTION_MIN_ROUNDS) in message
    assert "offline" in message


def test_the_floor_itself_is_accepted():
    """The bound is inclusive — 12 is the requirement, not just above it."""
    s = _settings(
        environment="production",
        jwt_secret="x" * 32,
        bcrypt_rounds=_BCRYPT_PRODUCTION_MIN_ROUNDS,
    )
    assert s.bcrypt_rounds == _BCRYPT_PRODUCTION_MIN_ROUNDS


def test_production_may_raise_the_work_factor():
    """The floor is a floor, not a fixed value — 14 must not be refused."""
    s = _settings(environment="production", jwt_secret="x" * 32, bcrypt_rounds=14)
    assert s.bcrypt_rounds == 14


def test_prod_is_spelled_both_ways():
    """``ENVIRONMENT=prod`` must not be a way around the floor."""
    with pytest.raises(ValidationError, match="BCRYPT_ROUNDS"):
        _settings(environment="prod", bcrypt_rounds=4)
    with pytest.raises(ValidationError, match="BCRYPT_ROUNDS"):
        _settings(environment="PRODUCTION", bcrypt_rounds=4)


@pytest.mark.parametrize("rounds", [_BCRYPT_MIN_ROUNDS - 1, _BCRYPT_MAX_ROUNDS + 1, 0])
def test_a_work_factor_outside_bcrypts_range_is_refused_anywhere(rounds):
    """Not a production rule: bcrypt raises on these, so catch it at startup.

    Left to ``gensalt`` it would surface as a ValueError on the first
    registration rather than on the boot that introduced it.
    """
    with pytest.raises(ValidationError, match="BCRYPT_ROUNDS"):
        _settings(environment="development", bcrypt_rounds=rounds)


@pytest.mark.parametrize("env", ["development", "staging", "test"])
def test_the_low_work_factor_is_allowed_outside_production(env):
    s = _settings(environment=env, bcrypt_rounds=_BCRYPT_MIN_ROUNDS)
    assert s.bcrypt_rounds == _BCRYPT_MIN_ROUNDS


def test_the_default_is_the_production_floor():
    """Nothing has to be set for a box to be safe — the default already is.

    The floor is enforced rather than trusted, but an install that never names
    BCRYPT_ROUNDS at all should land on the same value the guard would demand.
    """
    assert Settings.model_fields["bcrypt_rounds"].default == (
        _BCRYPT_PRODUCTION_MIN_ROUNDS
    )


def test_the_suite_actually_runs_at_the_low_work_factor():
    """Guards the reason the setting exists.

    If conftest's BCRYPT_ROUNDS ever stops taking effect, the suite silently
    goes back to spending minutes in bcrypt and nothing fails — it just gets
    slow again, which is the failure mode that started this.
    """
    from app.config import settings

    assert settings.bcrypt_rounds == _BCRYPT_MIN_ROUNDS
    assert _cost_of(hash_password("hunter2hunter2")) == _BCRYPT_MIN_ROUNDS


def test_a_hash_made_at_the_low_factor_still_round_trips():
    """The cheap path must be the same path, not a different one."""
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong horse battery staple", hashed)


def test_an_existing_production_hash_still_verifies_at_the_low_setting():
    """Lowering the setting must not strand hashes already in the database.

    bcrypt encodes the cost in the hash, so verification uses whatever the hash
    was made with and ignores the current setting entirely. This is what makes
    the work factor safe to change on a live box: it re-prices new hashes only.
    Asserted rather than assumed, because the whole migration story rests on it.
    """
    from app.security import _prepare

    expensive = bcrypt.hashpw(
        _prepare("hunter2hunter2"), bcrypt.gensalt(rounds=6)
    ).decode("utf-8")
    assert _cost_of(expensive) == 6  # made at a cost the suite does not use

    assert verify_password("hunter2hunter2", expensive)
    assert not verify_password("something else", expensive)
