"""The migration chain has to stay a chain, and has to build the models' schema.

``deploy/deploy.sh`` and ``docker-compose.yml`` both run ``alembic upgrade
head``, singular. Alembic refuses that command outright when two revisions have
no descendant — "Multiple head revisions are present for given argument 'head'"
— so a second head is not a merge to resolve later, it is a deploy that stops
between ``systemctl stop pulse-worker`` and ``systemctl restart pulse-api``.
The API comes back up against a schema the models have moved past.

Getting there takes nothing exotic: write a new revision, set ``down_revision``
to the migration that *looks* newest by filename or by Create Date, and miss
that a later one already claimed it.

The gap underneath both of these is the same one. Every test in the suite runs
against ``Base.metadata.create_all`` on in-memory SQLite, so the whole of
``alembic/versions`` is dead weight as far as the suite is concerned: a column
added to a model with no migration written for it passes 4,000 tests and 500s
in production on the first query that names it. The last test here is the one
that closes it — it applies the real chain to a real (temporary) database and
compares the result against the models.
"""
from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database import Base

BACKEND = pathlib.Path(__file__).resolve().parents[1]
VERSIONS = BACKEND / "alembic" / "versions"


@pytest.fixture(scope="module")
def scripts() -> ScriptDirectory:
    config = Config(str(BACKEND / "alembic.ini"))
    # ``script_location`` in the ini is relative to backend/, which is the
    # working directory for the alembic CLI but not for pytest.
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(config)


def test_there_is_exactly_one_head(scripts):
    """``alembic upgrade head`` must have one place to go."""
    heads = scripts.get_heads()

    assert len(heads) == 1, (
        "two revisions have no descendant, so `alembic upgrade head` — what "
        "deploy.sh runs — fails instead of migrating. Point the newer one's "
        "down_revision at the other: " + ", ".join(sorted(heads))
    )


def test_every_revision_file_is_on_the_path_from_base_to_head(scripts):
    """No revision sits outside the chain the deploy actually walks.

    A file alembic loads but never reaches is a migration that will not run.
    With one head the usual cause is a duplicated ``revision`` id, where the
    second file quietly shadows the first and its ``upgrade()`` never executes.
    """
    walked = {revision.revision for revision in scripts.walk_revisions("base", "heads")}
    files = sorted(path.name for path in VERSIONS.glob("*.py"))

    assert len(walked) == len(files), (
        f"{len(files)} revision files on disk, {len(walked)} reachable from "
        "base to head — one of them is shadowed or orphaned"
    )


def test_the_chain_builds_the_schema_the_models_declare(tmp_path):
    """Apply every migration to an empty database and diff it against the models.

    The check the suite could not otherwise make: nothing else here runs a
    migration at all. Compared on table and column names rather than on types —
    a type diff across two dialects is a study in false positives, while a
    missing column is unambiguous and is what a forgotten migration looks like.

    In a subprocess because ``alembic/env.py`` takes its URL from
    ``settings.database_url``, which this process has already resolved to
    in-memory SQLite; the environment is the only way to point it somewhere a
    second connection can read.
    """
    database = tmp_path / "chain.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": f"sqlite:///{database}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(database)
    migrated = {
        name: {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        # Alembic's own bookkeeping table, which is not in the models.
        if name != "alembic_version"
    }
    conn.close()

    declared = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.tables.values()
    }

    assert set(declared) - set(migrated) == set(), (
        "the models declare tables no migration creates: "
        + ", ".join(sorted(set(declared) - set(migrated)))
    )
    missing_columns = {
        name: sorted(columns - migrated[name])
        for name, columns in declared.items()
        if columns - migrated.get(name, set())
    }
    assert missing_columns == {}, (
        "the models declare columns no migration adds, so a deployed database "
        f"will not have them: {missing_columns}"
    )
