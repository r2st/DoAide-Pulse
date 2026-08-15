"""Herald had no scheduled backup, and a delete is a real ``DELETE``.

``routers.content.delete_content`` removes the row and its publications
outright — there is no soft-delete column and no trash to restore from. The only
copies of the database that existed before this were the ad-hoc ``pg_dump`` in
``deploy/DEPLOYMENT.md``'s rollback section, taken by hand "before anything
risky", so the newest one on the box was whenever somebody last remembered.

``deploy/backup.sh`` plus ``herald-backup.timer`` close that. None of it is
Python, and a backup job fails in the one way nobody notices — quietly, at
03:30, until the morning somebody needs it. So the properties that make the
difference between having backups and believing you do are asserted here:

* the dump is **verified** by reading it back before it counts as one;
* an interrupted run leaves nothing that **looks** like a backup;
* retention can never delete the last copy;
* the job cannot write over the application it is backing up.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
_SCRIPT = _DEPLOY / "backup.sh"
_SERVICE = _DEPLOY / "systemd" / "herald-backup.service"
_TIMER = _DEPLOY / "systemd" / "herald-backup.timer"


def _directive(unit: str, key: str) -> str | None:
    """The last value assigned to *key*, the way systemd reads a unit file."""
    found = re.findall(rf"^{key}=(.*)$", unit, flags=re.MULTILINE)
    return found[-1].strip() if found else None


def _int_directive(unit: str, key: str) -> int:
    """*key* as an integer, failing the test rather than the type checker."""
    raw = _directive(unit, key)
    assert raw is not None, f"{key} is not set"
    return int(raw)


def _code(script: str) -> str:
    """The script with its comments stripped.

    Half of ``backup.sh`` is prose explaining what it deliberately does *not*
    do, so a bare substring search over the whole file finds ``pg_dumpall`` in
    the sentence saying it is never used.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the script with a scratch environment and no inherited DATABASE_URL."""
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HERALD_BACKUP_DIR": str(tmp_path),
            # Pointed at a file that does not exist, so a test that means to
            # reach the connection logic has to say so itself. Without this the
            # script would read the developer's real /opt/Herald/.env if they
            # happen to have one.
            "HERALD_ENV_FILE": str(tmp_path / "absent.env"),
            **env,
        },
    )


# --------------------------------------------------------------------------- #
# The files exist and are wired together                                       #
# --------------------------------------------------------------------------- #


def test_the_backup_script_is_executable():
    """``ExecStart=`` runs it directly, so the bit is load-bearing, not tidiness."""
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111, f"{_SCRIPT} is not executable"


def test_the_service_runs_the_script_the_repo_ships():
    """A unit pointing at a path the repo does not have is a backup that never runs."""
    exec_start = _directive(_SERVICE.read_text(), "ExecStart")

    assert exec_start == "/opt/Herald/deploy/backup.sh"
    # deploy.sh rsyncs the repo to /opt/Herald, so that path is this file.
    assert _SCRIPT == _DEPLOY / "backup.sh"


def test_the_timer_starts_the_backup_service():
    """``Unit=`` has to name the service; a timer with a typo fires nothing."""
    assert _directive(_TIMER.read_text(), "Unit") == "herald-backup.service"


def test_the_timer_is_installed_into_the_timer_target():
    """Without ``WantedBy=``, ``systemctl enable`` has nothing to link and the
    timer silently never starts at boot."""
    assert _directive(_TIMER.read_text(), "WantedBy") == "timers.target"


def test_a_backup_missed_while_the_box_was_down_is_taken_on_boot():
    """``Persistent=true``.

    The box reboots for kernel updates. Without this a backup whose window
    passed during the reboot is simply skipped until tomorrow — and the backup
    somebody wants is disproportionately the one from the day something
    unusual was happening to the machine.
    """
    assert _directive(_TIMER.read_text(), "Persistent") == "true"


# --------------------------------------------------------------------------- #
# The job cannot damage what it is backing up                                  #
# --------------------------------------------------------------------------- #


def test_the_backup_cannot_write_into_the_application_directory():
    """``ProtectSystem=strict`` plus a ``ReadWritePaths`` that names only the
    dump directory.

    The three application units list ``/opt/Herald`` because they write there.
    This one only reads the script and the env file, and a backup process that
    can write over the application it is dumping has the failure modes of both.
    """
    unit = _SERVICE.read_text()

    assert _directive(unit, "ProtectSystem") == "strict"
    assert _directive(unit, "ReadWritePaths") == "/var/backups/herald"


def test_the_backup_reads_the_same_env_file_as_the_application():
    """One ``DATABASE_URL``, so the dump cannot drift onto a different database.

    Naming the database a second time in the unit is the configuration that
    looks right for years and turns out to have been dumping an empty
    development database the whole time.
    """
    assert _directive(_SERVICE.read_text(), "EnvironmentFile") == "/opt/Herald/.env"


def test_a_failed_dump_is_not_retried_into_a_loop():
    """``Restart=no``.

    A dump that fails at 03:30 fails for a reason that will still be true at
    03:31 — a full disk, a database that is down. Retrying turns one failure
    into a night of them and buries the cause; the timer comes back tomorrow.
    """
    assert _directive(_SERVICE.read_text(), "Restart") == "no"


def test_the_dump_yields_to_everything_else_on_the_box():
    """Six products share this host and the dump is the only job with no deadline."""
    unit = _SERVICE.read_text()

    assert _int_directive(unit, "Nice") > 0
    assert _directive(unit, "IOSchedulingClass") == "idle"


def test_the_dump_names_one_database_rather_than_dumping_the_cluster():
    """``pg_dumpall`` would put five other products' data in Herald's file.

    Postgres on this box is shared. A cluster-wide dump owned by Herald's
    service account is a data-protection problem, not a thorough backup.
    """
    code = _code(_SCRIPT.read_text())

    assert "pg_dumpall" not in code
    assert "pg_dump --format=custom" in code


# --------------------------------------------------------------------------- #
# The dump is verified, and a partial one never counts as a backup             #
# --------------------------------------------------------------------------- #


def test_the_dump_is_read_back_before_it_counts_as_a_backup():
    """``pg_dump`` can exit 0 and still have written a truncated file when the
    disk filled underneath it. ``pg_restore --list`` is the cheap proof."""
    body = _SCRIPT.read_text()

    assert "pg_restore --list" in body
    # And the verification has to happen before the rename, or it is checking a
    # file that has already been published as a backup.
    assert body.index("pg_restore --list") < body.index('mv "$partial" "$target"')


def test_the_dumps_are_private_to_the_service_account():
    """``umask 077``. Six products share this box and a dump is the database.

    Every password hash and every encrypted platform credential Herald holds is
    in that file. It is also the reason the restore runbook has to stage a copy
    — see the test below, which is the other half of this one.
    """
    assert "umask 077" in _code(_SCRIPT.read_text())


def test_the_documented_restore_does_not_hand_postgres_a_file_it_cannot_read():
    """The runbook has to work as written, on the dumps the script actually makes.

    ``umask 077`` puts the dumps at ``0600`` in a ``0700`` directory owned by
    ``herald``, so ``sudo -u postgres pg_restore /var/backups/herald/…`` — which
    is what DEPLOYMENT.md used to say — fails with ``Permission denied``. That is
    the wrong failure at the worst time: it arrives during an incident, from the
    one command nobody has rehearsed, and it reads as a corrupt backup rather
    than as a mode bit.

    Dropping to ``postgres`` is what makes it a problem, so that is what this
    checks: any command that does both must name the staged copy, not the vault.

    Continuations are joined first, and that is not a detail — the runbook line
    this was written for wrapped the path onto the next line with a ``\\``, so a
    scan that reads the file line by line finds ``sudo -u postgres`` and the
    path in different strings and passes on the very command it exists to catch.
    """
    body = _DEPLOY.joinpath("DEPLOYMENT.md").read_text()
    # One shell command per element, however it was wrapped for the page.
    commands = body.replace("\\\n", " ").splitlines()

    offenders = [
        command.strip()
        for command in commands
        if "sudo -u postgres" in command and "/var/backups/herald/" in command
    ]

    assert not offenders, (
        "DEPLOYMENT.md tells the reader to run pg_restore as `postgres` against "
        "a dump only `herald` can read: " + " | ".join(offenders)
    )


def test_the_restore_drill_would_notice_a_partial_restore():
    """``pg_restore`` exits 0 on a restore that threw errors the whole way down.

    A drill without ``--exit-on-error`` is a drill that passes on a broken dump,
    which is the same false confidence the nightly verification exists to avoid.
    """
    body = _DEPLOY.joinpath("DEPLOYMENT.md").read_text()

    assert "--exit-on-error" in body


def test_an_interrupted_run_leaves_nothing_that_looks_like_a_backup():
    """Written as ``*.dump.partial`` and renamed only after it verifies.

    The retention sweep matches ``*.dump`` and the operator reaching for the
    newest file matches it by eye. Either one picking up a half-written dump is
    the failure this naming exists to prevent.
    """
    body = _SCRIPT.read_text()

    assert 'partial="$target.partial"' in body
    assert "trap 'rm -f \"$partial\"' EXIT" in body


def test_retention_can_never_delete_the_last_copy():
    """``-mtime +N`` with ``N`` at zero matches the file just written.

    A retention of 0 is a configuration mistake — "keep nothing" is not a backup
    policy anyone means — and the script refuses it rather than carrying it out.
    """
    body = _SCRIPT.read_text()

    assert '[ "$RETENTION_DAYS" -ge 1 ]' in body
    # The guard has to precede the delete, not merely exist.
    assert body.index('"$RETENTION_DAYS" -ge 1') < body.index("-delete")


def test_the_password_is_never_passed_on_the_command_line():
    """Six products can read ``ps`` on this box.

    libpq's PG* environment variables keep the password out of argv; a
    ``postgres://user:pass@…`` URI handed to pg_dump would put it there.
    """
    code = _code(_SCRIPT.read_text())

    assert "PGPASSWORD" in code
    invocations = [
        line
        for line in code.splitlines()
        if "pg_dump " in line or "pg_restore " in line
    ]
    assert invocations, "no pg_dump/pg_restore invocation found"
    assert not [line for line in invocations if "DATABASE_URL" in line]


# --------------------------------------------------------------------------- #
# The guards actually fire                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_it_refuses_to_run_with_no_database_url(tmp_path):
    """The env file is missing and nothing is in the environment.

    This is what a misconfigured unit looks like, and the script has to fail
    loudly rather than dump whatever libpq's defaults happen to reach.
    """
    result = _run(tmp_path)

    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
    assert list(tmp_path.glob("*.dump")) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_it_refuses_a_database_url_it_cannot_parse(tmp_path):
    """A URL naming no host or no database is a typo, not a connection."""
    result = _run(tmp_path, DATABASE_URL="not-a-url")

    assert result.returncode != 0
    assert list(tmp_path.glob("*.dump")) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_it_refuses_to_write_into_a_directory_that_is_not_there(tmp_path):
    """``ProtectSystem=strict`` means a mistyped ``ReadWritePaths`` shows up as a
    missing directory. Failing here is how that gets noticed on day one rather
    than on the day of the restore."""
    missing = tmp_path / "nope"
    result = _run(
        missing,
        DATABASE_URL="postgresql+psycopg://u:p@127.0.0.1:5432/herald",
    )

    assert result.returncode != 0
    assert "does not exist" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_it_reads_the_database_url_out_of_the_env_file_without_sourcing_it(tmp_path):
    """``/opt/Herald/.env`` is systemd's format, not shell.

    Sourcing it would execute whatever a future value contained. The proof is
    that a line which *would* run as shell does not: the marker file it would
    create is absent, and the script goes on to use the URL on the next line.
    """
    env_file = tmp_path / "herald.env"
    marker = tmp_path / "sourced"
    env_file.write_text(
        f"EVIL=$(touch {marker})\n"
        "DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:5432/herald\n"
    )

    result = _run(tmp_path, HERALD_ENV_FILE=str(env_file))

    assert not marker.exists(), "backup.sh sourced the env file as shell"
    # It got past the URL parse — whatever happened next was Postgres refusing
    # the connection, not the script failing to read its configuration.
    assert "is not a URL this can dump" not in result.stderr
