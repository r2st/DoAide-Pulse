"""``.env.example`` is a template for production, so its defaults must be safe.

Nothing reads this file at runtime, which is exactly why it drifts. It is the
thing a deploy copies to ``/opt/Herald/.env`` and then edits — and what does not
get edited is whatever already looked deliberate. It shipped ``DEBUG=true`` for
that entire time.

The rule these tests encode: a value in the example is either safe to leave
alone in production, or it is an obvious placeholder that refuses to boot there.
``JWT_SECRET`` is the second kind — the config validator rejects it by name, so
copying it unedited stops the deploy instead of quietly weakening it. ``DEBUG``
was neither, which is what made it dangerous rather than merely wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

#: Repo root — tests/ -> backend/ -> Herald/
_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _example_values() -> dict[str, str]:
    """Parse the example into a plain dict, dropping comments and blanks."""
    values: dict[str, str] = {}
    for raw in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Trailing `# development | production` style comments are annotations,
        # not part of the value.
        values[key.strip()] = value.split("#")[0].strip()
    return values


def test_the_example_exists_and_parses():
    """Guards the parser above: an empty dict would make every test below pass."""
    assert _ENV_EXAMPLE.is_file(), f"{_ENV_EXAMPLE} is missing"
    values = _example_values()
    assert values, "parsed nothing out of .env.example"
    assert values["APP_NAME"] == "Herald"


def test_debug_is_off_in_the_example():
    """The one that was actually wrong.

    ``DEBUG=true`` in production reopens /docs and, worse, takes Herald's
    catch-all 500 handler out of circuit so Starlette answers unhandled
    exceptions with a traceback page — source lines and frame locals to whoever
    triggered it. It gains nothing here: /docs is already served whenever
    ENVIRONMENT is not production.
    """
    assert _example_values()["DEBUG"] == "false"


def test_the_example_environment_is_development():
    """So a half-edited copy fails closed rather than claiming to be production.

    An example that said `production` would pair a production environment with
    the placeholder secrets below it — and the JWT guard would stop the boot,
    but only after the operator had been told this file was ready to use.
    """
    assert _example_values()["ENVIRONMENT"] == "development"


def test_copying_the_example_unedited_cannot_start_in_production():
    """The backstop for everything this file does not think to check.

    If someone copies the example, sets ENVIRONMENT=production and edits nothing
    else, Herald must refuse to boot. The placeholder JWT secret is what makes
    that true, and it is worth pinning: a well-meaning change to a "nicer"
    default secret would turn a loud failure into a silent one.
    """
    from pydantic import ValidationError

    example = _example_values()
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=example["JWT_SECRET"],
            database_url="sqlite://",
        )


#: Keys the example documents that are deliberately not ``Settings`` fields,
#: because something reads them straight from the environment instead. These are
#: one-shot bootstrap values for ``python -m app.seed``, not app configuration —
#: the running API has no use for them and should not carry them.
_READ_DIRECTLY_FROM_ENV = {"SEED_EMAIL", "SEED_PASSWORD"}


def test_every_example_key_is_a_real_setting():
    """A key the app does not read is a line an operator will set and trust.

    ``extra="ignore"`` means a renamed or misspelled setting here is accepted in
    silence, so the example can go on documenting a knob that stopped existing.
    """
    known = {name.upper() for name in Settings.model_fields} | _READ_DIRECTLY_FROM_ENV
    unknown = sorted(set(_example_values()) - known)
    assert not unknown, f".env.example documents settings that do not exist: {unknown}"


def test_the_directly_read_keys_are_still_read_directly():
    """Keeps the allowlist above honest.

    Each exception is a claim that some module reads the name out of os.environ
    itself. If that stops being true — the seed grows a real setting, or the
    reads move — the name belongs back under the check rather than parked in a
    list nobody revisits.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "seed.py").read_text(
        encoding="utf-8"
    )
    for key in _READ_DIRECTLY_FROM_ENV:
        assert f'"{key}"' in source, f"{key} is no longer read by app/seed.py"
        assert key.lower() not in Settings.model_fields, (
            f"{key} is now a real setting — drop it from the allowlist"
        )


# --------------------------------------------------------------------------- #
# Draft preview links                                                          #
# --------------------------------------------------------------------------- #


def test_the_preview_link_knobs_are_documented():
    """All three, or the section is a trap.

    The retention sweep is the only one of the three an operator has a reason to
    change — it is the one that decides how long the account keeps rows for links
    that stopped working — and it is meaningless without the ceiling it has to
    clear. Documenting the TTLs and omitting the sweep would leave
    ``GET /content/{id}/preview-links`` quietly growing for the life of the
    account with nothing in the file to suggest a knob exists.
    """
    values = _example_values()
    for key in (
        "PREVIEW_LINK_DEFAULT_TTL_HOURS",
        "PREVIEW_LINK_MAX_TTL_HOURS",
        "PREVIEW_LINK_RETENTION_DAYS",
    ):
        assert key in values, f".env.example does not document {key}"


def test_a_live_preview_link_can_never_be_swept():
    """The invariant the retention comment claims, checked rather than asserted in prose.

    ``maintenance_tasks`` prunes any link whose row is older than the retention
    window, and it does not ask whether the link still opens. So retention has to
    outlive the longest TTL an author may request, or a reviewer's link is
    deleted while it is still inside its own ``expires_at`` — a 404 on a link the
    UI is still listing as live.

    Both halves are checked: the example (what a deploy copies) and the
    ``Settings`` defaults (what an install that never edits the file runs with).
    A change to either alone is the one that would break this.
    """
    values = _example_values()
    example_retention_hours = int(values["PREVIEW_LINK_RETENTION_DAYS"]) * 24
    assert example_retention_hours > int(values["PREVIEW_LINK_MAX_TTL_HOURS"])

    defaults = Settings(_env_file=None, jwt_secret="x" * 40, database_url="sqlite://")
    assert (
        defaults.preview_link_retention_days * 24 > defaults.preview_link_max_ttl_hours
    )


def test_the_example_default_ttl_is_one_a_caller_could_have_asked_for():
    """A default above the maximum would be silently clamped on every issue.

    ``preview_links.issue`` applies ``min(hours, max_ttl_hours)`` to whatever it
    is given, including the default. An example that shipped a default larger
    than the max would therefore document a TTL no link ever gets, and the
    mismatch would only ever show up as links expiring earlier than the file
    says.
    """
    values = _example_values()
    assert int(values["PREVIEW_LINK_DEFAULT_TTL_HOURS"]) <= int(
        values["PREVIEW_LINK_MAX_TTL_HOURS"]
    )
