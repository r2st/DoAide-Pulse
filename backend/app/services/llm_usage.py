"""Writing and reading :class:`app.models.llm_usage.LLMUsage`.

Two functions and a deliberate asymmetry between them.

:func:`record` is called from inside :mod:`app.services.llm_router`, on the path
of every completion, in whichever process made the call. It opens its own
session because the router has none — it is a pure HTTP client, and threading a
``Session`` through ``complete`` → ``_sweep`` → ``_call`` to write one
bookkeeping row would put the database in the signature of every LLM call in
Pulse, including the ones made from request handlers that already hold a
different session.

:func:`summary` is called once, from ``/api/v1/metrics``, with the caller's
session. It aggregates in SQL rather than loading rows because the window it
covers is a day of them — a day of completions is a lot of rows to load in
order to add up five columns, and the endpoint is on a dashboard that polls.

The endpoint is **authenticated** (bearer token, like every other read here).
An earlier draft of this module planned it open and said so in two places; that
would have published token spend and per-provider failure rates to anyone who
asked, which is operational detail about the install rather than about any
caller. What survives from that plan is the SQL aggregation, which is worth
keeping on its own merits.

**record never raises.** Not "should not" — the ``except Exception`` is
unconditional and the reason is worth being explicit about, because a blanket
handler is normally a smell. The caller is holding a *finished completion*: text
a provider was paid for in quota, on the free tier, where the quota is the
scarce thing. Anything this function could raise — the table is missing because
a migration has not run, the session cannot connect, a column is too narrow —
is a fact about bookkeeping, and letting it propagate would discard the
completion and make the next attempt spend the quota again. So the failure is
logged at WARNING and the completion goes back to its caller.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.llm_usage import (
    MODEL_MAX_LENGTH,
    PROVIDER_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
    LLMUsage,
)
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)


def tokens(usage: Any, key: str) -> int | None:
    """One token count out of a provider's ``usage`` block, or ``None``.

    Everything here is somebody else's JSON. The block is absent on several of
    the free compat layers, present-but-null on others, and at least one returns
    the counts as strings. ``None`` for anything that is not a non-negative
    integer — see the model's note on why a zero would be worse than a null.
    """
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool):  # bool is an int; a flag here is not a count
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def record(
    *,
    provider: str,
    model: str,
    purpose: str = "",
    ok: bool,
    duration_ms: int,
    usage: Any = None,
    session_factory: Any = None,
) -> None:
    """Write one attempt's accounting. Never raises — see the module docstring.

    *usage* is the provider's raw ``usage`` block, passed through unparsed so
    the coercion lives in one place. *session_factory* exists for the tests,
    which hand in the fixture session rather than the process-wide one.
    """
    factory = session_factory or SessionLocal
    try:
        db = factory()
    except Exception as exc:  # pragma: no cover - defensive; see the docstring
        logger.warning("llm usage not recorded (no session): %s", exc)
        return
    try:
        db.add(
            LLMUsage(
                provider=(provider or "")[:PROVIDER_MAX_LENGTH],
                model=(model or "")[:MODEL_MAX_LENGTH],
                purpose=(purpose or "")[:PURPOSE_MAX_LENGTH],
                ok=bool(ok),
                # Negative is not a duration. A monotonic clock cannot go
                # backwards, but the value arrives from a caller and a stored
                # negative would make an average lie in a direction nobody
                # checks.
                duration_ms=max(0, int(duration_ms)),
                prompt_tokens=tokens(usage, "prompt_tokens"),
                completion_tokens=tokens(usage, "completion_tokens"),
                total_tokens=tokens(usage, "total_tokens"),
            )
        )
        db.commit()
    except Exception as exc:
        logger.warning("llm usage not recorded for %s/%s: %s", provider, model, exc)
        # Suppressed, not handled. A session that could not commit frequently
        # cannot roll back either — the connection is what broke — and there is
        # nothing left to salvage or report that the line above has not said.
        with contextlib.suppress(Exception):
            db.rollback()
    finally:
        with contextlib.suppress(Exception):
            db.close()


def summary(db: Session, *, hours: int = 24) -> dict[str, Any]:
    """Token spend and latency over the last *hours*, overall and per provider.

    One grouped query plus one total, rather than a query per provider: the
    provider list is configuration and could grow.

    ``avg_duration_ms`` counts *every* attempt including failed ones, which is
    the number an operator wants — a provider timing out at ninety seconds is
    the reason generation got slow, and an average over successes only would
    hide exactly that. ``calls_failed`` sits beside it so the two can be read
    together.
    """
    since = utcnow() - timedelta(hours=hours)

    # `case` rather than a second filtered query: SQLite and PostgreSQL both
    # take it, and one pass over the window is one pass.
    failed = func.sum(case((LLMUsage.ok.is_(False), 1), else_=0))

    rows = db.execute(
        select(
            LLMUsage.provider,
            func.count(LLMUsage.id),
            failed,
            func.sum(LLMUsage.total_tokens),
            func.sum(LLMUsage.prompt_tokens),
            func.sum(LLMUsage.completion_tokens),
            func.avg(LLMUsage.duration_ms),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(LLMUsage.provider)
        .order_by(LLMUsage.provider)
    ).all()

    providers = [
        {
            "provider": provider,
            "calls": int(calls or 0),
            "calls_failed": int(failures or 0),
            "total_tokens": int(total or 0),
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "avg_duration_ms": int(round(float(avg_ms or 0))),
        }
        for provider, calls, failures, total, prompt, completion, avg_ms in rows
    ]

    return {
        "window_hours": hours,
        "calls": sum(p["calls"] for p in providers),
        "calls_failed": sum(p["calls_failed"] for p in providers),
        "total_tokens": sum(p["total_tokens"] for p in providers),
        "prompt_tokens": sum(p["prompt_tokens"] for p in providers),
        "completion_tokens": sum(p["completion_tokens"] for p in providers),
        # Re-derived from the rows rather than averaged over `providers`: the
        # mean of three provider means is not the mean, and the three are
        # weighted very differently when one of them serves almost everything.
        "avg_duration_ms": _weighted_mean(providers),
        "by_provider": providers,
    }


def by_purpose(db: Session, *, hours: int = 24) -> list[dict[str, Any]]:
    """The same window as :func:`summary`, grouped by what the call was *for*.

    Split out from ``summary`` rather than added to it because the two answer
    different questions and only one of them is per-provider. ``summary`` says
    which upstream the tokens went to — the quota question. This says which
    feature spent them, which is the only place "average generation time" can
    honestly come from: Pulse stores no generation duration on ``Content``, so
    the number is the mean wall-clock of the completions tagged
    :data:`GENERATION_PURPOSE`, and tagging is done by the callers listed in
    :mod:`app.models.llm_usage`'s note on the column.

    Rows written before a caller started tagging carry ``""``. They are grouped
    as-is rather than dropped or folded into another bucket: an untagged call
    still spent the quota, and hiding it would make the purposes sum to less
    than the total ``summary`` reports for the same window.
    """
    since = utcnow() - timedelta(hours=hours)
    failed = func.sum(case((LLMUsage.ok.is_(False), 1), else_=0))

    rows = db.execute(
        select(
            LLMUsage.purpose,
            func.count(LLMUsage.id),
            failed,
            func.sum(LLMUsage.total_tokens),
            func.avg(LLMUsage.duration_ms),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(LLMUsage.purpose)
        .order_by(LLMUsage.purpose)
    ).all()

    return [
        {
            "purpose": purpose or "",
            "calls": int(calls or 0),
            "calls_failed": int(failures or 0),
            "total_tokens": int(total or 0),
            "avg_duration_ms": int(round(float(avg_ms or 0))),
        }
        for purpose, calls, failures, total, avg_ms in rows
    ]


#: The ``purpose`` tag :mod:`app.services.content_generator` writes when it
#: generates a piece. Named here rather than spelled as a literal at the reader
#: because the metrics endpoint reports its duration as "average generation
#: time", and a typo would report a confident zero rather than an error.
GENERATION_PURPOSE = "content"


def _weighted_mean(providers: list[dict[str, Any]]) -> int | None:
    """The per-call mean duration across providers, weighted by call count.

    ``None`` when no calls have been recorded — 0 would show as the fastest
    possible install on a dashboard rather than as missing data.
    """
    calls = sum(p["calls"] for p in providers)
    if not calls:
        return None
    total = sum(p["avg_duration_ms"] * p["calls"] for p in providers)
    return int(round(total / calls))


def purge(db: Session, *, days: int) -> int:
    """Delete usage rows older than *days*. Commits. Returns the row count.

    Bounded by age rather than by row count — see the model docstring.
    """
    cutoff = utcnow() - timedelta(days=days)
    deleted = db.execute(
        LLMUsage.__table__.delete().where(LLMUsage.created_at < cutoff)
    ).rowcount
    db.commit()
    return int(deleted or 0)


__all__ = ["GENERATION_PURPOSE", "by_purpose", "purge", "record", "summary", "tokens"]
