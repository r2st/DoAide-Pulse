"""A warning must fail the run, in both places that configure the suite.

Herald tracks FastAPI, Starlette, SQLAlchemy and PyJWT closely, and the way any
of those tells you a call is about to stop working is a warning on the run that
uses it. Both config files used to open with ``ignore::DeprecationWarning``, so
that notice went to ``/dev/null`` for however many releases it took for the
feature to actually disappear — at which point the message is a traceback and
the deprecation period was spent for nothing.

Two files have to agree, which is the other half of this: ``pytest`` from
``backend/`` reads ``backend/pyproject.toml`` and ``pytest`` from the repo root
reads ``pytest.ini``. Tightening one and not the other leaves a green run
available to anyone who cd's to the other directory.

These tests read configuration rather than behaviour, so they are worth exactly
what the reasoning above is worth and no more: they cannot tell you the filter
works, only that nobody has widened it back. ``test_a_warning_actually_fails``
covers the other half by raising one.
"""
from __future__ import annotations

import configparser
import tomllib
import warnings
from pathlib import Path

import pytest

#: tests/ -> backend/ -> Herald/
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "backend" / "pyproject.toml"
_PYTEST_INI = _REPO_ROOT / "pytest.ini"


def _pyproject_filters() -> list[str]:
    with _PYPROJECT.open("rb") as fh:
        config = tomllib.load(fh)
    return config["tool"]["pytest"]["ini_options"]["filterwarnings"]


def _ini_filters() -> list[str]:
    parser = configparser.ConfigParser()
    # `;` comments only — configparser treats `#` as an inline comment marker
    # and the file's header block uses `;` throughout for that reason.
    parser.read(_PYTEST_INI, encoding="utf-8")
    raw = parser["pytest"]["filterwarnings"]
    return [line.strip() for line in raw.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("name", "filters"),
    [
        ("backend/pyproject.toml", _pyproject_filters()),
        ("pytest.ini", _ini_filters()),
    ],
)
def test_the_first_filter_turns_warnings_into_errors(name, filters):
    """``error`` has to come first — filters are applied in order, last wins.

    A bare ``error`` anywhere later in the list would be strictly worse than
    useless: it would override the targeted ignores above it and turn exactly
    the warnings someone had already justified back into failures.
    """
    assert filters, f"{name} declares no warning filters at all"
    assert filters[0] == "error", (
        f"{name} must open its filterwarnings list with `error`; found {filters[0]!r}"
    )


@pytest.mark.parametrize(
    ("name", "filters"),
    [
        ("backend/pyproject.toml", _pyproject_filters()),
        ("pytest.ini", _ini_filters()),
    ],
)
def test_no_filter_ignores_a_whole_category(name, filters):
    """Silencing one known warning is a decision; silencing a class is a habit.

    ``ignore::DeprecationWarning`` reads like housekeeping and covers every
    deprecation any dependency will ever emit, including the ones that arrive
    next year from code nobody has looked at. A specific ``ignore:<message>:``
    entry is fine and expected — it names what was silenced, so the next person
    can check whether it is still true.
    """
    for entry in filters[1:]:
        if not entry.startswith("ignore"):
            continue
        action, _, rest = entry.partition(":")
        message = rest.partition(":")[0]
        assert message, (
            f"{name} carries a blanket {entry!r}; ignore a specific message "
            "instead, so the entry says what it is hiding and stops applying "
            "once that message is gone"
        )
        assert action == "ignore"


def test_a_warning_actually_fails():
    """The filter is live in this process, not merely written down.

    Both files could say the right thing while a plugin or a ``-W`` on the
    command line put it back; this asserts on the behaviour the rest of the
    module only describes.
    """
    with pytest.raises(UserWarning, match="herald-warning-filter-probe"):
        warnings.warn("herald-warning-filter-probe", UserWarning, stacklevel=1)


def test_a_deprecation_fails_too():
    """DeprecationWarning is the category the old config named by hand.

    Python's own default filters hide it outside ``__main__``, so it is the one
    that most needs the explicit ``error`` — and the one whose regression would
    be least visible.
    """
    with pytest.raises(DeprecationWarning, match="herald-deprecation-probe"):
        warnings.warn("herald-deprecation-probe", DeprecationWarning, stacklevel=1)
