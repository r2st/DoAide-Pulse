"""Editing a unit file has to be the same act as deploying it.

``deploy.sh`` rsyncs the repo to ``/opt/Herald`` and restarts four services.
systemd does not read ``/opt/Herald/deploy/systemd`` — it reads
``/etc/systemd/system`` — so for as long as installing was a manual step
documented in ``DEPLOYMENT.md``, a unit file could be edited, reviewed, tested,
committed and deployed while the box went on running the copy from whenever
somebody last remembered.

That is not hypothetical. Two rounds of graceful-shutdown work shipped exactly
that way: the ``--timeout-graceful-shutdown`` on ``herald-api`` and the raised
``TimeoutStopSec`` on ``herald-worker`` were both live in the repo, asserted by
``tests/test_shutdown_is_graceful.py``, and absent from the running units. The
tests passed. The deploy said ``done``. The drain window did not exist.

The failure mode is what makes it worth pinning: nothing is red. ``systemctl
status`` reports the installed copy, ``git`` reports the edited one, and neither
of them is looking at the other. So the invariants asserted here are about the
deploy script, which is the only place the two can be made to meet:

* every unit under ``deploy/systemd`` is installed, not an enumerated subset —
  a list to keep in step is the same class of bug one level up;
* installed as a copy owned by root, because the deploy has just made the source
  writable by the service account;
* and ``daemon-reload`` runs when, and only when, something changed.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "deploy" / "deploy.sh"
_UNITS = _REPO / "deploy" / "systemd"

#: The install loop, as the script actually spells it. Everything below reads
#: this block rather than the whole file, so an assertion cannot be satisfied by
#: a matching string somewhere else — a comment, or the rsync above it.
_LOOP = re.compile(
    r"^for source in (?P<glob>.+?); do$(?P<body>.*?)^done$",
    re.MULTILINE | re.DOTALL,
)


@pytest.fixture(scope="module")
def script() -> str:
    return _DEPLOY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def install_loop(script: str) -> re.Match[str]:
    match = _LOOP.search(script)
    assert match, "deploy.sh no longer installs unit files at all"
    return match


def test_the_deploy_installs_unit_files(install_loop):
    """The whole point: systemd's copy comes from the deploy, not from memory."""
    body = install_loop.group("body")

    assert "/etc/systemd/system/" in body
    assert "install " in body


def test_it_installs_whatever_is_in_the_directory_rather_than_a_list(install_loop):
    """A hard-coded list of units is the same drift one level up.

    ``herald-backup.timer`` is the case that proves it: it is not one of the
    four the deploy restarts, so any list written from the restart line would
    have silently left it out.
    """
    glob = install_loop.group("glob")

    assert "*.service" in glob
    assert "*.timer" in glob
    assert str(_UNITS.name) in glob


@pytest.mark.parametrize("unit", sorted(p.name for p in _UNITS.iterdir()))
def test_every_file_in_the_unit_directory_is_covered_by_the_glob(unit, install_loop):
    """The glob is only as good as what it actually matches.

    Checked against the patterns the script really expands, and against every
    file in the directory rather than every file with an extension already known
    to match — a ``.socket`` or a ``.path`` added later would be a unit the
    deploy silently walks past, which is the original bug wearing a new suffix.
    """
    # Compared on the basename: the patterns are rooted at the deploy target,
    # ``/opt/Herald/deploy/systemd``, and the directory being checked is the
    # same one in this checkout.
    patterns = [PurePosixPath(p).name for p in install_loop.group("glob").split()]

    assert any(fnmatch(unit, pattern) for pattern in patterns), (
        f"{unit} is in deploy/systemd and no pattern in deploy.sh would install it"
    )


def test_the_installed_copy_belongs_to_root(install_loop):
    """``deploy.sh`` chowns all of ``/opt/Herald`` to ``herald`` before this
    runs, so the source is writable by the service account. A unit file systemd
    executes as root, writable by the user that root's services run as, is a
    privilege escalation with a deploy script for a delivery mechanism."""
    body = install_loop.group("body")

    assert "-o root -g root" in body
    assert "-m 0644" in body


def test_nothing_is_symlinked_into_opt(script: str, install_loop):
    """A symlink would make the rsync at the top of the deploy a live edit of a
    unit systemd is already running."""
    assert " ln -s" not in script
    assert "ln -s" not in install_loop.group("body")


def test_a_changed_unit_is_reloaded(script: str):
    """Installing a file systemd has not re-read changes nothing at all."""
    assert "systemctl daemon-reload" in script


def test_the_reload_is_conditional_on_something_having_changed(script: str, install_loop):
    """``daemon-reload`` re-runs every generator on a box with six applications
    on it, and this script runs on every push. The comparison is what keeps an
    unchanged deploy from paying for it."""
    body = install_loop.group("body")
    assert "cmp -s" in body

    guarded = re.search(
        r'if \[ "\$units_changed" -eq 1 \]; then\s+systemctl daemon-reload', script
    )
    assert guarded, "daemon-reload is no longer guarded on a unit having changed"


def test_the_units_are_installed_before_anything_is_restarted(script: str):
    """Order is the difference between a fix that ships and one that ships next
    time. A restart that happens before the new unit is loaded runs the old
    one, and nothing restarts it again until the following deploy."""
    installed_at = script.index("/etc/systemd/system/")
    reloaded_at = script.index("systemctl daemon-reload")
    stopped_at = script.index("systemctl stop")
    restarted_at = script.index("systemctl restart")

    assert installed_at < reloaded_at < stopped_at < restarted_at


def test_the_units_are_installed_after_the_tree_is_synced(script: str):
    """They are copied *out* of the rsynced tree, so a deploy that installed
    first would install the previous deploy's units, one round behind, forever."""
    assert script.index("rsync") < script.index("/etc/systemd/system/")


def test_every_unit_the_deploy_restarts_exists_in_the_repo(script: str):
    """The other direction: a restart line naming a unit nothing installs is a
    unit whose file lives only on the box."""
    named = set()
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith(("systemctl restart ", "systemctl start ")):
            named.update(stripped.split()[2:])

    assert named, "deploy.sh no longer restarts anything"
    for unit in named:
        assert (_UNITS / f"{unit}.service").is_file(), (
            f"deploy.sh restarts {unit}, which has no unit file in deploy/systemd"
        )
