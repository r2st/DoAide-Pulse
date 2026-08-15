"""Every completion attempt leaves a row, and no row costs a completion.

The file :mod:`app.services.llm_usage` and
``tests/test_llm_router_response_edges.py`` both name. Two properties, and the
second is the one worth having a file for.

**Attempts are recorded, not just successes.** The table exists to answer "what
did the quota go on, and why did generation get slow", and a provider timing out
at the ninety-second ceiling is the biggest single answer to the second
question. It contributes no tokens and would be invisible in a table of served
completions — see :func:`app.services.llm_usage.summary`, whose
``avg_duration_ms`` deliberately counts refused attempts.

**Bookkeeping never costs a completion.** ``record`` swallows everything it can
raise, on purpose and unconditionally, because the caller is holding text a
provider was already paid for in quota. A blanket ``except Exception`` is
normally a smell, so the reason is pinned here rather than left to the
docstring: if it is ever narrowed, the tests that fail should be the ones that
say why it was blanket.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.llm_usage import LLMUsage
from app.services import llm_router, llm_usage


class _FakeResponse:
    """Just enough of ``httpx.Response`` for :func:`llm_router._call`."""

    def __init__(self, payload: dict, *, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


def _completion(text: str, *, usage: dict | None = None) -> dict:
    payload: dict = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


@pytest.fixture
def one_provider(monkeypatch):
    """Groq configured, nothing else, no fallback models."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "key-groq")
    monkeypatch.setattr(settings, "groq_fallback_models", "")


@pytest.fixture(autouse=True)
def _fresh_llm_breaker():
    """The router's breaker is a module-level singleton with in-process state.

    Not covered by conftest's autouse reset, which is the *publishing* breaker —
    this one is reset per-file by the modules that provoke it, and a test here
    that trips it would otherwise have the next test's provider skipped before
    it was ever asked.
    """
    llm_router.breaker.reset()
    yield
    llm_router.breaker.reset()


@pytest.fixture
def usage_session(db, monkeypatch):
    """Point ``llm_usage.record``'s own session factory at the test database.

    ``record`` opens its own session on purpose — the router has none to give it
    — so a test that does not do this writes to the process-wide factory and
    asserts against an empty fixture database.
    """

    class _NoClose:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr(llm_usage, "SessionLocal", lambda: _NoClose())
    return db


def _rows(db) -> list[LLMUsage]:
    return list(db.query(LLMUsage).order_by(LLMUsage.id))


# --------------------------------------------------------------------------- #
# A served completion                                                          #
# --------------------------------------------------------------------------- #


def test_a_served_completion_writes_its_tokens(one_provider, usage_session, monkeypatch):
    """The row carries the provider's own usage block, coerced."""
    monkeypatch.setattr(
        llm_router,
        "_post",
        lambda *a, **k: _FakeResponse(
            _completion(
                "written",
                usage={"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
            )
        ),
    )

    result = llm_router.complete([{"role": "user", "content": "hi"}], purpose="content")

    assert result.text == "written"
    (row,) = _rows(usage_session)
    assert row.provider == "groq"
    assert row.ok is True
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (11, 22, 33)
    assert row.purpose == "content"


def test_a_provider_that_reports_no_usage_records_nulls_not_zeros(
    one_provider, usage_session, monkeypatch
):
    """Several free compat layers omit the block entirely.

    A zero would drag a sum down while looking like a measurement — the
    reasoning :mod:`app.models.llm_usage` gives for the columns being nullable.
    The row is still written: the call happened and its duration is real.
    """
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **k: _FakeResponse(_completion("written"))
    )

    llm_router.complete([{"role": "user", "content": "hi"}])

    (row,) = _rows(usage_session)
    assert row.ok is True
    assert row.prompt_tokens is None
    assert row.completion_tokens is None
    assert row.total_tokens is None


# --------------------------------------------------------------------------- #
# A refused attempt                                                            #
# --------------------------------------------------------------------------- #


def test_a_failed_attempt_is_recorded_with_its_duration(
    one_provider, usage_session, monkeypatch
):
    """The property the whole table is for.

    A refused attempt spent no tokens and did spend time. Recording only the
    served ones would hide exactly the provider that is making generation slow.
    """
    monkeypatch.setattr(
        llm_router,
        "_post",
        lambda *a, **k: _FakeResponse({}, status_code=500, text="upstream is unwell"),
    )

    # ``AllProvidersFailed``, not ``LLMError``: the per-attempt errors are
    # collected by the sweep and re-raised as one chain-level failure.
    with pytest.raises(llm_router.AllProvidersFailed):
        llm_router.complete([{"role": "user", "content": "hi"}])

    rows = _rows(usage_session)
    assert rows, "a refused attempt left no row; the failure is invisible"
    assert all(row.ok is False for row in rows)
    assert all(row.total_tokens is None for row in rows)
    assert all(row.duration_ms >= 0 for row in rows)


def test_a_failed_attempt_and_a_served_one_both_land(
    monkeypatch, usage_session
):
    """A chain that fails over records both halves, in order.

    Two rows against one ``Completion``: ``summary``'s ``calls`` counts
    attempts, which is what makes its ``calls_failed`` beside it readable.
    """
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "key-gemini")
    monkeypatch.setattr(settings, "groq_api_key", "key-groq")
    monkeypatch.setattr(settings, "gemini_fallback_models", "")
    monkeypatch.setattr(settings, "groq_fallback_models", "")

    def fake_post(url, *, json, headers, timeout):
        if "generativelanguage" in url:
            return _FakeResponse({}, status_code=500, text="gemini is unwell")
        return _FakeResponse(_completion("groq wrote this"))

    monkeypatch.setattr(llm_router, "_post", fake_post)

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    assert result.provider == "groq"
    rows = _rows(usage_session)
    assert [(r.provider, r.ok) for r in rows] == [("gemini", False), ("groq", True)]


# --------------------------------------------------------------------------- #
# Bookkeeping never costs a completion                                         #
# --------------------------------------------------------------------------- #


def test_a_broken_usage_table_does_not_discard_the_completion(
    one_provider, monkeypatch
):
    """The reason ``record``'s ``except Exception`` is blanket.

    The caller is holding finished text a provider was paid for in quota. If
    this ever raises, the completion is discarded and the next attempt spends
    the quota again — a bookkeeping error that destroys work.
    """

    def exploding_factory():
        raise RuntimeError("no such table: llm_usage")

    monkeypatch.setattr(llm_usage, "SessionLocal", exploding_factory)
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **k: _FakeResponse(_completion("written"))
    )

    result = llm_router.complete([{"role": "user", "content": "hi"}])

    assert result.text == "written"


def test_a_commit_that_fails_does_not_discard_the_completion(one_provider, monkeypatch):
    """The other half: the session opens and the write is what fails."""

    class _BadSession:
        def add(self, _row):
            pass

        def commit(self):
            raise RuntimeError("connection reset")

        def rollback(self):
            # A session that could not commit frequently cannot roll back
            # either. ``record`` suppresses this too, and a test that let it
            # pass would not be exercising the suppression.
            raise RuntimeError("connection reset")

        def close(self):
            pass

    monkeypatch.setattr(llm_usage, "SessionLocal", _BadSession)
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **k: _FakeResponse(_completion("written"))
    )

    assert llm_router.complete([{"role": "user", "content": "hi"}]).text == "written"


def test_a_failure_to_record_is_logged_at_warning(one_provider, monkeypatch, caplog):
    """Swallowed is not silent — the operator has to be able to find out."""
    monkeypatch.setattr(
        llm_usage, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    monkeypatch.setattr(
        llm_router, "_post", lambda *a, **k: _FakeResponse(_completion("written"))
    )

    with caplog.at_level("WARNING", logger="app.services.llm_usage"):
        llm_router.complete([{"role": "user", "content": "hi"}])

    assert any("not recorded" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Column widths and coercion                                                   #
# --------------------------------------------------------------------------- #


def test_an_over_long_provider_name_is_truncated_rather_than_raising(usage_session):
    """PostgreSQL answers an over-long INSERT with an exception nothing catches.

    ``record`` truncates to the declared widths on the way in. The values come
    from configuration a user edits, so this is reachable without a code change.
    """
    llm_usage.record(
        provider="p" * 200,
        model="m" * 400,
        purpose="x" * 200,
        ok=True,
        duration_ms=5,
        session_factory=lambda: usage_session,
    )

    (row,) = _rows(usage_session)
    assert len(row.provider) == 40
    assert len(row.model) == 120
    assert len(row.purpose) == 40


def test_a_negative_duration_is_stored_as_zero(usage_session):
    """A stored negative would make an average lie in a direction nobody checks."""
    llm_usage.record(
        provider="groq",
        model="m",
        ok=True,
        duration_ms=-5,
        session_factory=lambda: usage_session,
    )

    (row,) = _rows(usage_session)
    assert row.duration_ms == 0


@pytest.mark.parametrize(
    "block,expected",
    [
        ({"total_tokens": 10}, 10),
        # At least one compat layer returns the counts as strings.
        ({"total_tokens": "10"}, 10),
        ({"total_tokens": " 10 "}, 10),
        ({"total_tokens": -1}, None),
        ({"total_tokens": None}, None),
        ({"total_tokens": "many"}, None),
        # bool is an int in Python; a flag here is not a count.
        ({"total_tokens": True}, None),
        ({}, None),
        ("not a dict", None),
        (None, None),
    ],
)
def test_a_usage_block_is_read_defensively(block, expected):
    """Everything in the block is somebody else's JSON."""
    assert llm_usage.tokens(block, "total_tokens") == expected
