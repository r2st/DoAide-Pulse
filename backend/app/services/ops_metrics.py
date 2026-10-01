"""The aggregates behind ``/api/v1/metrics`` — how the *install* is running.

Named ``ops_metrics`` rather than ``metrics`` to keep it apart from
:mod:`app.models.metrics`, which is :class:`~app.models.metrics.ContentMetric`
— the per-post view and engagement snapshots that
:mod:`app.services.analytics_service` reads. Those are metrics about *posts*,
for the person who wrote them. These are metrics about *Pulse*, for the person
running it, and the two never appear in the same response.

**Two scopes in one payload, and the split is deliberate.**

Everything derived from a table with an owner on it — content counts, scan
frequency, publish outcomes — is filtered to the calling account, the same way
every other read in the API is. Nothing here is a way to see somebody else's
rows.

The LLM and breaker sections cannot be scoped that way and are not pretended
to be. :class:`~app.models.llm_usage.LLMUsage` has no ``user_id``: it is
written by :mod:`app.services.llm_router`, which is a pure HTTP client that
does not know whose request it is serving, and adding the column would mean
threading an account through every call site to answer a question — "is the
install about to run out of free-tier quota" — that is not per-account in the
first place. So the token and breaker numbers describe the deployment, and on a
multi-account install every authenticated caller sees the same ones. On the
single-tenant deployment Pulse actually ships as, that distinction is
invisible; it is written down here because it stops being invisible the moment
somebody adds a second account.

**The breaker section describes one process.** ``CircuitBreaker`` is in-memory
and per-process — see :mod:`app.services.breaker`, which explains why that is
the right trade. The process answering this request is the API, and the process
that generates content is a Celery worker, so an empty ``breakers`` block here
does not mean no provider is standing down; it means none is standing down *in
the web process*. This is the same caveat ``/health/detail`` carries, and it is
why the LLM numbers beside it are read from a table instead: the table is the
part that crosses the process boundary.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.content import Content, ContentStatus
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.services import llm_router, llm_usage
from app.services.publishers import breaker as publishers_breaker


def content_counts(db: Session, user_id: int) -> dict[str, Any]:
    """How many pieces this account has, per status.

    Every status is present, including the ones at zero. A caller charting this
    should not have to know Pulse's status vocabulary to discover that
    ``failed`` is missing because nothing failed, rather than because the key
    was renamed — and a bar chart that drops its empty categories re-orders
    itself as the data changes.
    """
    rows = db.execute(
        select(Content.status, func.count(Content.id))
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .group_by(Content.status)
    ).all()

    counted = {status.value: int(count or 0) for status, count in rows}
    by_status = {status.value: counted.get(status.value, 0) for status in ContentStatus}
    return {"total": sum(by_status.values()), "by_status": by_status}


def scan_frequency(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Per project: how often its repo has actually been scanned, and how slowly.

    Read off the counters :meth:`app.models.project.Project.record_scan`
    maintains rather than from a table of scan rows — that is the trade the
    model documents, and it is why this costs one query however many scans have
    happened.

    ``scans_per_day`` is over the project's whole lifetime, not a recent window,
    because that is what the stored counter can support: ``scan_count`` is a
    total and ``created_at`` is when counting started. It answers "is the beat
    schedule doing what it was configured to do" — a project registered a
    fortnight ago and scanned twice has an autopilot that is not running —
    and deliberately not "has it slowed down this week", which needs a series
    this schema does not keep.

    A project created moments ago is reported as ``None`` rather than as a
    spectacular rate: dividing a scan or two by a fraction of a day produces a
    number that looks like a measurement and is an artefact of the denominator.
    """
    projects = db.execute(
        select(
            Project.id,
            Project.name,
            Project.scan_count,
            Project.last_scanned_at,
            Project.last_scan_duration_ms,
            Project.created_at,
        )
        .where(Project.user_id == user_id)
        .order_by(Project.id)
    ).all()

    out = []
    for row in projects:
        age_days = _age_days(row.created_at)
        out.append(
            {
                "project_id": row.id,
                "name": row.name,
                "scan_count": int(row.scan_count or 0),
                "last_scanned_at": row.last_scanned_at,
                "last_scan_duration_ms": row.last_scan_duration_ms,
                "scans_per_day": (
                    round(int(row.scan_count or 0) / age_days, 2)
                    if age_days >= 1.0
                    else None
                ),
            }
        )
    return out


def _age_days(created_at: Any) -> float:
    """How long ago *created_at* was, in days. ``0.0`` if it is missing.

    Through :func:`app.models.mixins.as_aware` because the column comes back
    naive on SQLite and aware on PostgreSQL, and subtracting one from an aware
    "now" raises — which would mean this worked in production and failed in the
    tests, or the reverse.
    """
    if created_at is None:
        return 0.0
    return max(0.0, (utcnow() - as_aware(created_at)).total_seconds() / 86400.0)


def publish_rates(db: Session, user_id: int) -> dict[str, Any]:
    """Publication outcomes and platform latency for this account.

    Counted over every publication row the account has, not a window: a
    success rate is a property of the whole history, and a rate over "today"
    on an install that published nothing today is a zero that reads like a
    failure.

    The denominator is *settled* rows only — published and failed. A row still
    pending, scheduled or publishing has not succeeded or failed yet, and
    counting it as a non-success would make the rate dip every time something
    was queued and recover when it landed, which is motion with no information
    in it. ``in_flight`` is reported beside the rate so the rows left out are
    visible rather than merely absent.

    ``avg_duration_ms`` is the other half of "how is this platform behaving",
    and it is the half that moves first. A destination that is about to start
    failing gets slow before it starts refusing, and a success rate cannot show
    that: it is 1.0 right up until the moment it is not. The mean is over the
    rows that have a duration — see
    :attr:`app.models.publication.Publication.duration_ms`, which is NULL for a
    row no attempt has reached a platform for, including every row written
    before the column existed — and ``timed`` is reported beside it so a mean
    over two attempts is not read as a measurement of the platform.
    """
    rows = db.execute(
        select(
            Publication.platform,
            func.sum(
                case((Publication.status == PublicationStatus.PUBLISHED, 1), else_=0)
            ),
            func.sum(
                case((Publication.status == PublicationStatus.FAILED, 1), else_=0)
            ),
            func.count(Publication.id),
            # ``count`` of a nullable column counts the non-NULLs, which is
            # exactly the denominator ``avg`` used — so the two always agree,
            # and a platform with no timed attempt reports ``timed: 0`` beside a
            # null mean rather than a zero that reads as instant.
            func.count(Publication.duration_ms),
            func.avg(Publication.duration_ms),
        )
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .group_by(Publication.platform)
        .order_by(Publication.platform)
    ).all()

    by_platform = [
        {
            "platform": platform.value,
            "published": int(published or 0),
            "failed": int(failed or 0),
            "in_flight": int(total or 0) - int(published or 0) - int(failed or 0),
            "success_rate": _rate(int(published or 0), int(failed or 0)),
            "timed": int(timed or 0),
            "avg_duration_ms": (
                int(round(float(avg_ms))) if timed and avg_ms is not None else None
            ),
        }
        for platform, published, failed, total, timed, avg_ms in rows
    ]

    published = sum(p["published"] for p in by_platform)
    failed = sum(p["failed"] for p in by_platform)
    return {
        "published": published,
        "failed": failed,
        "in_flight": sum(p["in_flight"] for p in by_platform),
        "success_rate": _rate(published, failed),
        "timed": sum(p["timed"] for p in by_platform),
        # Weighted by each platform's timed attempts rather than a mean of the
        # means: one Bluesky post and four hundred Dev.to posts are not two
        # equal opinions about how long a publish takes.
        "avg_duration_ms": _weighted_mean(by_platform),
        "by_platform": by_platform,
    }


def _weighted_mean(by_platform: list[dict[str, Any]]) -> int | None:
    """Mean platform latency across every timed attempt, or ``None`` if none.

    ``None`` rather than ``0`` for the same reason :func:`_rate` returns it: an
    install that has never timed a publish has not got an average latency of
    zero milliseconds, and zero is the one value that would look like the
    fastest possible install on a dashboard.
    """
    timed = sum(p["timed"] for p in by_platform)
    if not timed:
        return None
    total = sum(p["avg_duration_ms"] * p["timed"] for p in by_platform if p["timed"])
    return int(round(total / timed))


def _rate(published: int, failed: int) -> float | None:
    """Share of settled attempts that succeeded, or ``None`` if none have.

    ``None`` rather than ``0.0`` for "nothing has settled yet": a fresh install
    has not got a success rate of zero percent, it has not got one at all, and
    the two render very differently on a dashboard.
    """
    settled = published + failed
    if not settled:
        return None
    return round(published / settled, 4)


def llm_section(db: Session, *, hours: int) -> dict[str, Any]:
    """Token spend, per-provider and per-purpose, plus average generation time.

    Install-wide — see the module docstring for why this one cannot be scoped
    to the caller.
    """
    summary = llm_usage.summary(db, hours=hours)
    purposes = llm_usage.by_purpose(db, hours=hours)
    generation = next(
        (p for p in purposes if p["purpose"] == llm_usage.GENERATION_PURPOSE), None
    )
    return {
        **summary,
        "by_purpose": purposes,
        # Nulls, not zeros, when nothing was generated in the window. "The
        # average generation took 0ms" is the one reading that is certainly
        # wrong, and it is what a dashboard would plot.
        "avg_generation_ms": generation["avg_duration_ms"] if generation else None,
        "generations": generation["calls"] if generation else 0,
    }


def breaker_section() -> dict[str, Any]:
    """Which upstreams this process is currently standing down.

    See the module docstring: *this process*, which is the API. Both snapshots
    are reported even when empty, because an absent key and an empty one would
    otherwise be indistinguishable from a breaker that has never tripped.
    """
    return {
        "scope": "api-process",
        "llm": llm_router.breaker.snapshot(),
        "publish": publishers_breaker.snapshot(),
    }


def build(db: Session, user_id: int, *, hours: int) -> dict[str, Any]:
    """The whole payload. One call per section, no section reading another."""
    return {
        "window_hours": hours,
        "content": content_counts(db, user_id),
        "projects": scan_frequency(db, user_id),
        "publishing": publish_rates(db, user_id),
        "llm": llm_section(db, hours=hours),
        "breakers": breaker_section(),
    }


__all__ = [
    "breaker_section",
    "build",
    "content_counts",
    "llm_section",
    "publish_rates",
    "scan_frequency",
]
