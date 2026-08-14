"""Type hints and docstrings on the public surface, enforced rather than remembered.

The sibling rule to :mod:`tests.test_every_endpoint_is_documented`, one layer
down: that file governs what the HTTP contract says about itself, this one
governs what the Python one does. Both exist for the same reason — the failure
mode is silence. A parameter that loses its annotation does not break anything,
it just stops being checkable, and a public helper that arrives with no
docstring reads as deliberate until somebody counts.

"Public" means a module-level function, or a method on a class, whose name does
not start with an underscore, anywhere under ``app/``. Private helpers are
exempt: they are read next to their only caller, and the argument for
documenting every one of them is the argument that produced the docstrings
nobody reads.
"""
from __future__ import annotations

import ast
import pathlib

#: Arguments that are never annotated and never should be.
_IMPLICIT = {"self", "cls"}

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"


def _public_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every public function and method under ``app/``, with a readable name.

    Walks the module body and one level into classes rather than using
    ``ast.walk``: a closure defined inside a function is not public surface,
    however it is spelled.
    """
    out = []
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT.parent)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    out.append((f"{rel}:{node.lineno} {node.name}", node))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(
                        sub, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and not sub.name.startswith("_"):
                        out.append(
                            (f"{rel}:{sub.lineno} {node.name}.{sub.name}", sub)
                        )
    return out


def test_the_walk_finds_the_surface_it_claims_to():
    """A guard on the guard.

    Both tests below pass vacuously if this collection ever returns nothing —
    a rename under ``app/`` or a parse failure would turn the whole file green
    while checking nothing at all.
    """
    found = _public_functions()
    assert len(found) > 200, f"only {len(found)} public functions found"
    names = {name.split()[-1] for name, _ in found}
    assert "publish_content" in names  # a router endpoint
    assert "Totals.to_dict" in names  # a method on a dataclass


def test_every_public_function_annotates_its_arguments_and_return():
    missing: list[str] = []
    for name, node in _public_functions():
        args = node.args.args + node.args.kwonlyargs + node.args.posonlyargs
        unhinted = [
            a.arg for a in args if a.annotation is None and a.arg not in _IMPLICIT
        ]
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None and extra.annotation is None:
                unhinted.append(f"*{extra.arg}")
        if unhinted:
            missing.append(f"{name} — unannotated: {', '.join(unhinted)}")
        if node.returns is None:
            missing.append(f"{name} — no return annotation")

    assert not missing, "\n".join(missing)


def test_every_public_function_has_a_docstring():
    """What it says, not that it exists — but the second is what can be checked.

    A one-liner that restates the signature satisfies this and helps nobody, so
    the bar the reviewer applies is higher than the bar here. This only catches
    the case nothing else does: a public function with nothing at all.
    """
    missing = [name for name, node in _public_functions() if ast.get_docstring(node) is None]
    assert not missing, "\n".join(missing)


def test_a_docstring_of_only_whitespace_does_not_count():
    """``ast.get_docstring`` strips, so a blank one reads as absent — pin it.

    Worth pinning because the alternative reading is plausible: a function
    whose docstring is ``" "`` would otherwise pass the test above while
    documenting nothing.
    """
    tree = ast.parse('def f() -> None:\n    """   """\n')
    assert ast.get_docstring(tree.body[0]) == ""
