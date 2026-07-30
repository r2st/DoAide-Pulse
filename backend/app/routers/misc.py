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
from app.schemas.settings import DependencyOut, HealthOut
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


def _short(exc: BaseException) -> str:
    """One line of cause, truncated — this response is public."""
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:200]


@router.get("/health", response_model=HealthOut)
def health(response: Response, db: Session = Depends(get_db)) -> HealthOut:
    """Liveness, plus enough state to answer "why isn't X working?".

    Caddy polls this as its `health_uri`, so the status code is load-bearing: a
    non-2xx pulls this uvicorn out of the upstream pool. It therefore reports
    503 only for a dependency Herald genuinely cannot work without.

    * **Postgres — always required.** Every authenticated request touches it.
    * **Redis — required when ``CELERY_ENABLED``.** With workers running, a
      dead Redis means generation and publishing are silently queued nowhere;
      better to fail the check than to accept work that will never happen. In a
      single-process deployment (and in tests) everything runs inline, so Redis
      being down is reported but not fatal.

    The breaker state is per-process, so this reports the API process's view —
    workers keep their own. Enough to answer "is AI degraded right now?".
    """
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

    return HealthOut(
        status="ok" if healthy else "degraded",
        database=DependencyOut(
            status="ok" if database.ok else "unavailable",
            required=True,
            detail=database.detail,
        ),
        redis=DependencyOut(
            status="ok" if redis_probe.ok else "unavailable",
            required=redis_required,
            detail=redis_probe.detail,
        ),
        llm_providers=llm_router.configured_providers(),
        llm_breakers_open=llm_router.breaker.snapshot(),
        github_configured=bool(settings.github_token),
        credential_encryption=encryption_enabled(),
        implemented_platforms=[p.value for p in publishers.implemented_platforms()],
    )
