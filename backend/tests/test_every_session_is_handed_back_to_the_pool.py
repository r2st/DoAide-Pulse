"""A session that is not closed is a connection the pool never gets back.

Herald runs four processes against one PostgreSQL — two uvicorn workers, a
Celery worker and beat — at ``db_pool_size`` 5 plus ``db_max_overflow`` 10 each.
That is a ceiling of 60 connections against a ``max_connections`` of 100, and
the arithmetic only holds while every checkout is returned. One task that takes
a :data:`app.database.SessionLocal` and returns on a path that does not close it
leaks a connection per run; a beat task leaks one per tick, on a schedule, until
the pool is exhausted and every request in that process starts failing with
``QueuePool limit … connection timed out`` after a ``db_pool_timeout_seconds``
wait.

The failure has nothing useful in it. It surfaces far from the leak, in whatever
code was unlucky enough to ask for the next connection, and the leaking task
usually looks fine because it has been quietly working for weeks.

Every session in the tree is closed today. This is here so it stays that way: an
audit found no leak, and an audit is worth exactly as much as the guard it
leaves behind. Written against the AST rather than the text so that indentation,
comments and the shape of the surrounding code do not matter — only whether the
close is on a path that runs however the function exits.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"


def _sources() -> list[Path]:
    return sorted(p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts)


def _is_session_local_call(node: ast.AST) -> bool:
    """``SessionLocal()`` — however it was imported."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "SessionLocal"
    if isinstance(func, ast.Attribute):
        return func.attr == "SessionLocal"
    return False


def _closes(node: ast.AST, name: str) -> bool:
    """Whether *name*``.close()`` is called anywhere under *node*."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "close"
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == name
        ):
            return True
    return False


def _functions_taking_a_session() -> list[tuple[str, ast.AST, str]]:
    """Every function that takes a session, as ``(label, node, variable)``."""
    found: list[tuple[str, ast.AST, str]] = []
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for statement in ast.walk(node):
                if not isinstance(statement, ast.Assign):
                    continue
                if not _is_session_local_call(statement.value):
                    continue
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        label = f"{path.relative_to(_APP.parent)}:{node.lineno} {node.name}"
                        found.append((label, node, target.id))
    return found


TAKERS = _functions_taking_a_session()


def test_the_sweep_found_the_functions_it_is_meant_to_guard():
    """A sweep that matches nothing passes forever.

    Every Celery task in the tree opens its own session — there is no request to
    inherit one from — so the real number is well into double figures. If this
    drops to a handful, the AST walk has stopped seeing them and every
    assertion below has quietly become a no-op.
    """
    assert len(TAKERS) >= 15, f"only found {len(TAKERS)} SessionLocal() call sites"
    modules = {label.split(":")[0] for label, _, _ in TAKERS}
    assert "app/database.py" in modules
    assert any(module.startswith("app/tasks/") for module in modules)


@pytest.mark.parametrize(
    "label,node,variable",
    TAKERS,
    ids=[label for label, _, _ in TAKERS],
)
def test_a_session_is_closed_however_the_function_exits(label, node, variable):
    """The close has to be in a ``finally``, not merely present.

    A ``db.close()`` at the end of the happy path is the version of this bug
    that is hardest to see: it is there in the source, it runs in every test,
    and it is skipped on exactly the runs that matter — the ones that raised.
    Sweeps in this tree raise for real reasons (a provider outage, a failed
    write, a soft time limit), so "closed unless something went wrong" means
    "leaks whenever the system is already unhappy".

    ``with SessionLocal() as db`` would satisfy this too, and does not appear in
    the tree today; if it ever does, this reads the ``With`` as the
    close-on-exit it is.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Try) and any(
            _closes(statement, variable) for statement in child.finalbody
        ):
            return
        if isinstance(child, ast.With) and any(
            isinstance(item.optional_vars, ast.Name)
            and item.optional_vars.id == variable
            and _is_session_local_call(item.context_expr)
            for item in child.items
        ):
            return

    pytest.fail(
        f"{label} takes a session as `{variable}` and never closes it on an "
        "exit path that survives an exception — put the body in a `try:` with "
        f"`{variable}.close()` in its `finally:`, the way the other tasks do"
    )
