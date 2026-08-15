"""SQLAlchemy engine, session factory, and declarative base.

Uses SQLAlchemy 2.0 style. The engine URL comes from settings, so tests can
override it with an in-memory SQLite database.
"""
from __future__ import annotations

import math
from collections.abc import Generator, Iterable
from typing import TypeVar

from sqlalchemy import Engine, create_engine, inspect, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_Row = TypeVar("_Row", bound=Base)


def refresh_all(db: Session, instances: Iterable[_Row]) -> None:
    """Reload a batch of committed rows in one query per model, not one per row.

    ``Session.refresh`` emits a SELECT per instance, so refreshing the
    publications a publish just wrote costs a round trip per platform. The
    counts are small — a piece goes to at most a handful of platforms — but the
    shape is an N+1 and grows with whatever the caller happens to pass.

    A single ``WHERE pk IN (...)`` with ``populate_existing`` overwrites the
    already-loaded instances in the identity map, which is what ``refresh``
    does one row at a time. The primary keys come from
    :func:`sqlalchemy.inspect`'s identity rather than from the attributes: the
    session expires everything on commit, so reading ``row.id`` to build the
    ``IN`` clause would emit the very SELECTs this exists to avoid.

    Instances with no identity — transient, or pending and not yet flushed —
    have no row to reload and are skipped. Mixed models are fine; each gets its
    own query.
    """
    by_model: dict[type[Base], list[tuple]] = {}
    for instance in instances:
        identity = inspect(instance).identity
        if identity is not None:
            by_model.setdefault(type(instance), []).append(identity)

    for model, identities in by_model.items():
        keys = inspect(model).primary_key
        if len(keys) != 1:  # pragma: no cover - every model here has an `id`
            continue
        db.scalars(
            select(model)
            .where(keys[0].in_([identity[0] for identity in identities]))
            .execution_options(populate_existing=True)
        ).all()


def _statement_timeout_option(seconds: float) -> dict[str, str]:
    """libpq ``options`` that arm PostgreSQL's own statement timeout.

    Sent at connect time rather than per session, so it covers every checkout of
    every connection in the pool including the ones a background task takes —
    there is no code path that can forget it. In milliseconds because that is
    what ``statement_timeout`` takes, and rounded up so a sub-millisecond
    setting cannot become ``0``, which is PostgreSQL's spelling for *no limit*
    and the opposite of what asking for a tiny one meant.
    """
    if seconds <= 0:
        return {}
    return {"options": f"-c statement_timeout={math.ceil(seconds * 1000)}"}


#: libpq's own floor. ``connect_timeout=1`` is silently read as 2, and 0 means
#: "wait forever" — so a setting below this either does not mean what it says or
#: means the opposite of it.
MIN_CONNECT_TIMEOUT_SECONDS = 2


def _connect_timeout_arg(seconds: float) -> dict[str, int]:
    """A bound on *opening* a connection, which nothing else here provides.

    ``statement_timeout`` is a ceiling on a query, and a query cannot start
    until there is a connection to run it on. Getting one had no bound at all:
    ``pool_pre_ping`` discards a connection that has gone stale and opens a
    replacement, and if PostgreSQL is unreachable in the way that actually
    happens in production — a box that is up but dropping packets, rather than
    one refusing them — that open sits in the kernel's TCP retry schedule for
    over two minutes before libpq is told anything.

    Which makes it a *health check* problem before it is a request problem. The
    ``/health`` probe runs ``SELECT 1`` on a pooled session, so it inherits that
    wait; Caddy polls the same endpoint every 30s as its ``health_uri`` and
    gives up long before an answer arrives. The intended failure mode — a fast
    503 that says the database is unreachable, ejects the upstream, and gets an
    operator to the right dependency — was instead a probe that hung, two
    uvicorn workers stuck in a connect, and a site that timed out without ever
    saying why.

    Rounded up to a whole second because libpq's parameter has no finer
    resolution, and floored at :data:`MIN_CONNECT_TIMEOUT_SECONDS` because
    anything under it is not the setting it appears to be.
    """
    if seconds <= 0:
        return {}
    return {"connect_timeout": max(MIN_CONNECT_TIMEOUT_SECONDS, math.ceil(seconds))}


def _connect_options(seconds: float) -> dict[str, str]:
    """Every libpq startup setting Herald needs, as one ``connect_args``.

    ``timezone=UTC`` is here because Herald has exactly one clock and had no way
    of insisting on it. Timestamp columns are ``timestamptz``, and a driver
    reading one converts it to the *session* time zone before handing it back —
    a zone that comes from ``postgresql.conf``, the role, or the server's own
    locale, and that nothing in this repo was setting. So on a box that is not
    on UTC every timestamp arrived shifted, and the code that reads a field off
    one read the wrong field:

    * the dashboard's daily charts group readings by ``captured_at.date()`` and
      label their buckets from a UTC window, so near either end of the day a
      reading landed in a bucket the labels do not contain and was dropped from
      the chart without trace;
    * ``learned_cadence`` learns what hour of the day a post does well at from
      ``published.hour``, and hands it to a scheduler that means UTC — a whole
      publishing rhythm off by the server's offset.

    None of it shows in the tests: SQLite has no time zones and returns the
    naive UTC that was written. It is a bug that exists only where it is
    expensive, and it is one connection parameter.

    Merged with the timeout rather than sent separately because libpq takes a
    single ``options`` string; two keys would silently keep the last one.
    """
    settings_ = ["-c timezone=UTC"]
    timeout = _statement_timeout_option(seconds).get("options")
    if timeout:
        settings_.append(timeout)
    return {"options": " ".join(settings_)}


def _make_engine(url: str) -> Engine:
    # SQLite (tests) needs a special connect arg for multithreaded access, and
    # has no pool to tune: the in-memory database is one connection by
    # definition and the pool arguments below are rejected outright.
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        return create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args={"check_same_thread": False},
        )

    # `pool_pre_ping` catches connections that went stale mid-flight; the rest
    # is the per-process connection budget, which is a deployment-shaped number
    # and therefore configuration — see `app.config` for the arithmetic that
    # picks it.
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={
            **_connect_options(settings.db_statement_timeout_seconds),
            **_connect_timeout_arg(settings.db_connect_timeout_seconds),
        },
    )


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
