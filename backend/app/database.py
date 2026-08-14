"""SQLAlchemy engine, session factory, and declarative base.

Uses SQLAlchemy 2.0 style. The engine URL comes from settings, so tests can
override it with an in-memory SQLite database.
"""
from __future__ import annotations

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


def _make_engine(url: str) -> Engine:
    # SQLite (tests) needs a special connect arg for multithreaded access.
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    # Pool tuning for PostgreSQL.  pool_pre_ping catches connections that went
    # stale mid-flight; pool_recycle rotates them proactively so a PostgreSQL
    # restart or network blip does not accumulate dead connections in the pool.
    pool_kwargs = {} if is_sqlite else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 1800,  # 30 min
    }
    return create_engine(
        url, pool_pre_ping=True, future=True, connect_args=connect_args, **pool_kwargs
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
