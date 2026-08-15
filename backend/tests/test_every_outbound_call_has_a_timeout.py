"""Every outbound call in ``app/`` names a timeout, checked by reading the source.

All eleven call sites already pass one, so this fixes nothing. It exists because
of what the default is when somebody forgets: ``requests`` and ``smtplib`` wait
for ever, and a socket that never returns is not an error any retry, circuit
breaker or ``except`` clause in this codebase can see. The worker thread is
simply gone. Herald runs two uvicorn workers and a Celery pool, so a handful of
those is the whole application, and the symptom — everything hangs, nothing logs
— points nowhere near the line that caused it.

``httpx`` is better behaved (five seconds by default) but not by enough: this
codebase already decided what every one of these budgets should be, and they are
settings (``link_check_timeout_seconds``, ``feed_timeout_seconds``,
``webhook_timeout_seconds``, ``github_timeout_seconds``,
``publish_timeout_seconds``, ``smtp_timeout_seconds``) precisely so an operator
can move them. A call that silently takes the library's default is not
configurable and does not match the sibling call next to it.

**Source, not runtime.** A test that made the calls would need every one of them
to reach a socket, and the ones that matter most are the ones no test exercises.
The AST is the only view that sees a call nothing has run yet — which is exactly
the call this is for, since it is the *new* one that will forget.

**What counts as passing.** Either the call names ``timeout=``, or it is made
through a client that named it at construction: ``httpx.Client(timeout=...)``
sets the budget for every request issued through it, which is how
``link_check``, ``feeds`` and ``webhooks`` do it. So the rule below is about the
two places a timeout can be *set* — module-level request functions and client
constructors — and says nothing about ``client.get`` / ``client.stream``, which
inherit it and cannot be judged from one line of source.
"""
from __future__ import annotations

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"

#: ``module.attribute`` pairs that open a connection, by the module they are
#: reached through. ``requests`` and ``urllib`` are not used here today and are
#: listed anyway: they are what somebody reaches for out of habit, and they are
#: the two whose default is an unbounded wait.
OUTBOUND_CALLS: dict[str, set[str]] = {
    "httpx": {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "stream",
        "Client",
        "AsyncClient",
    },
    "requests": {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
    },
    "smtplib": {"SMTP", "SMTP_SSL", "LMTP"},
    "urllib": {"urlopen"},
    "request": {"urlopen"},  # ``from urllib import request``
}


def _outbound_calls(tree: ast.AST):
    """Yield ``(node, dotted_name)`` for every call that opens a connection.

    Matched on the attribute form (``httpx.post``) rather than on bare names,
    because that is the form this codebase uses everywhere and a bare
    ``post(...)`` is far more likely to be something local than an HTTP call.
    A ``from httpx import post`` would slip past; nothing does that here, and
    the import convention is itself worth keeping.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        module, attribute = func.value.id, func.attr
        if attribute in OUTBOUND_CALLS.get(module, ()):
            yield node, f"{module}.{attribute}"


def test_every_outbound_call_names_a_timeout():
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node, name in _outbound_calls(tree):
            keywords = {kw.arg for kw in node.keywords}
            # ``**kwargs`` arrives as ``arg=None``. Deliberately not accepted:
            # the point is to be able to read the budget off the call.
            if "timeout" in keywords:
                continue
            relative = path.relative_to(APP_ROOT.parent)
            offenders.append(f"{relative}:{node.lineno} {name}(...)")

    assert not offenders, (
        "outbound calls with no timeout: "
        + "; ".join(offenders)
        + ". Pass timeout= from a setting — a request with no deadline holds "
        "the worker thread for ever and reports nothing when it does."
    )


def test_the_sweep_can_tell_a_timeout_is_missing():
    """The rule above passes on a clean tree; this is what says it can fail.

    Without this, deleting the body of ``_outbound_calls`` would leave a green
    suite and no coverage at all — the failure mode of every sweep that only
    ever sees code that already complies.
    """
    tree = ast.parse(
        "import httpx\n"
        "httpx.get('https://example.com')\n"
        "httpx.post('https://example.com', timeout=5)\n"
        "httpx.Client()\n"
    )
    missing = [
        (name, node.lineno)
        for node, name in _outbound_calls(tree)
        if "timeout" not in {kw.arg for kw in node.keywords}
    ]
    assert missing == [("httpx.get", 2), ("httpx.Client", 4)]
