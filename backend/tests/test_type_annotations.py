"""Every function in ``app`` carries type annotations.

The tree was almost fully annotated already — eighteen gaps across sixty-odd
modules, all of them the kind that arrives one at a time and never gets noticed:
a ``call_next`` on a middleware, an ``info`` on a validator, a factory whose
return type is obvious to whoever wrote it that afternoon.

They are worth closing together and worth holding here rather than in a linter
config, because the failure mode is not "the code is wrong" — it is that the
annotation everyone assumes is present is the one place it is missing, and the
next reader trusts an inference the type checker never made.

``ruff`` is the repo's gate and does not have ANN enabled (turning it on would
flag the test suite too, where a bare ``monkeypatch`` argument is fine). This
sweep is scoped to ``app`` for exactly that reason.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

#: ``self`` and ``cls`` are the implicit receiver; annotating them says nothing.
IMPLICIT = {"self", "cls"}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _functions(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(APP.parent))


def test_there_are_modules_to_check():
    """The sweep is only as good as its file list.

    A path typo would leave every assertion below vacuously true, which is the
    one way a test like this fails silently.
    """
    modules = _modules()
    assert len(modules) > 30, f"only found {len(modules)} modules under {APP}"


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_every_function_annotates_its_return(path: pathlib.Path):
    missing = [
        f"{_relative(path)}:{fn.lineno}: {fn.name}"
        for fn in _functions(path)
        if fn.returns is None
    ]
    assert not missing, "functions with no return annotation:\n" + "\n".join(missing)


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_every_function_annotates_its_arguments(path: pathlib.Path):
    missing = []
    for fn in _functions(path):
        args = fn.args
        every = (
            args.posonlyargs
            + args.args
            + args.kwonlyargs
            + ([args.vararg] if args.vararg else [])
            + ([args.kwarg] if args.kwarg else [])
        )
        missing += [
            f"{_relative(path)}:{fn.lineno}: {fn.name}(... {arg.arg})"
            for arg in every
            if arg.annotation is None and arg.arg not in IMPLICIT
        ]
    assert not missing, "arguments with no annotation:\n" + "\n".join(missing)
