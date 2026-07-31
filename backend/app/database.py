"""SQLAlchemy engine, session factory, and declarative base.

Uses SQLAlchemy 2.0 style. The engine URL comes from settings, so tests can
override it with an in-memory SQLite database.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _make_engine(url: str):
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
