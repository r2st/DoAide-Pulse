"""Health check and platform capability discovery."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.settings import DependencyOut, HealthDetailOut, HealthOut
from app.services import llm_router, publishers
from app.services.crypto import encryption_enabled

logger = logging.getLogger(__name__)

router = APIRouter(tags=["misc"])


@dataclass(frozen=True)
class _Probe:
    ok: bool
    detail: str = ""


def _check_database(db: Session) -> _Probe:
    """Round-trip a trivial query.

    ``SELECT 1`` rather than merely holding a Session: the session factory does
    not connect until something is executed, so without a query this would
    report a dead Postgres as healthy.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return _Probe(False, _short(exc))
    return _Probe(True)


def _check_redis() -> _Probe:
    """PING the app Redis (DB 0). The Celery broker is the same server."""
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.health_check_timeout_seconds,
            socket_timeout=settings.health_check_timeout_seconds,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # redis raises a family of unrelated errors
        return _Probe(False, _short(exc))
    return _Probe(True)


def _short(exc: BaseException, *, public: bool = True) -> str:
    """One line of cause, truncated.

    When *public* is True (the default for the unauthenticated health endpoint)
    the detail is stripped to just the exception class name — internal messages
    and partial stack traces must not leak to anonymous callers.
    """
    cls = type(exc).__name__
    if public:
        return cls
    return f"{cls}: {exc}".splitlines()[0][:200]


def _health_core(response: Response, db: Session) -> tuple[bool, DependencyOut, DependencyOut]:
    """Shared probe logic for both health endpoints."""
    database = _check_database(db)
    redis_probe = _check_redis()
    redis_required = settings.celery_enabled

    healthy = database.ok and (redis_probe.ok or not redis_required)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning(
            "health check failed: database=%s redis=%s",
            database.detail or "ok",
            redis_probe.detail or "ok",
        )

    db_out = DependencyOut(
        status="ok" if database.ok else "unavailable",
        required=True,
        detail=database.detail,
    )
    redis_out = DependencyOut(
        status="ok" if redis_probe.ok else "unavailable",
        required=redis_required,
        detail=redis_probe.detail,
    )
    return healthy, db_out, redis_out


@router.get("/health", response_model=HealthOut)
def health(response: Response, db: Session = Depends(get_db)) -> HealthOut:
    """Public liveness probe.

    Caddy polls this as its ``health_uri``, so the status code is load-bearing.
    Returns only dependency reachability — no operational details.
    """
    healthy, db_out, redis_out = _health_core(response, db)
    return HealthOut(status="ok" if healthy else "degraded", database=db_out, redis=redis_out)


@router.get("/health/detail", response_model=HealthDetailOut)
def health_detail(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HealthDetailOut:
    """Authenticated detail view — answers "why isn't X working?".

    LLM provider config, circuit-breaker state, platform list, and encryption
    status are only visible to logged-in users.
    """
    healthy, db_out, redis_out = _health_core(response, db)
    return HealthDetailOut(
        status="ok" if healthy else "degraded",
        database=db_out,
        redis=redis_out,
        llm_providers=llm_router.configured_providers(),
        llm_breakers_open=llm_router.breaker.snapshot(),
        github_configured=bool(settings.github_token),
        credential_encryption=encryption_enabled(),
        implemented_platforms=[p.value for p in publishers.implemented_platforms()],
    )
