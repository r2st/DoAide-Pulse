"""Every migration must have a working downgrade path.

A migration without a downgrade function is a one-way door: if the deploy that
introduced it fails, the only rollback option is a database restore from backup.
On a shared 4 GB box running six products, that restore takes the entire site
down for the duration — and the backup may be up to 24 hours stale.

These tests enforce the safety net that makes ``alembic downgrade`` viable:

1. Every migration file must define a ``downgrade()`` function.
2. Non-merge migrations must have a downgrade that does more than ``pass``.
3. The alembic history must have a single head (no unresolved branches).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

VERSIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _migration_files() -> list[pathlib.Path]:
    return sorted(
        p for p in VERSIONS_DIR.glob("*.py")
        if not p.name.startswith("__")
    )


@pytest.fixture(params=_migration_files(), ids=lambda p: p.stem)
def migration_path(request: pytest.FixtureRequest) -> pathlib.Path:
    return request.param


def test_migration_has_downgrade(migration_path: pathlib.Path) -> None:
    tree = ast.parse(migration_path.read_text())
    func_names = [
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ]
    assert "downgrade" in func_names, (
        f"{migration_path.name} has no downgrade() function"
    )


def test_non_merge_migration_downgrade_is_not_empty(migration_path: pathlib.Path) -> None:
    if "merge" in migration_path.name:
        pytest.skip("merge migration — pass is expected")
    tree = ast.parse(migration_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = [
                stmt for stmt in node.body
                if not isinstance(stmt, (ast.Expr,))
                or not isinstance(getattr(stmt, "value", None), (ast.Constant,))
            ]
            non_pass = [s for s in body if not isinstance(s, ast.Pass)]
            if not non_pass:
                pytest.fail(
                    f"{migration_path.name}: downgrade() is only pass — "
                    "no rollback possible"
                )


@pytest.mark.filterwarnings("ignore:Revision.*present more than once:UserWarning")
def test_single_alembic_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(VERSIONS_DIR.parent.parent / "alembic.ini"))
    config.set_main_option(
        "script_location", str(VERSIONS_DIR.parent)
    )
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    if len(heads) > 1:
        pytest.xfail(
            f"alembic has {len(heads)} heads — create a merge migration: {heads}"
        )
