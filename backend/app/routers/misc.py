"""Health check and platform capability discovery."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.ratelimit import limiter
from app.schemas.errors import AUTHENTICATED, errors
from app.schemas.settings import DependencyOut, HealthDetailOut, HealthOut
from app.services import llm_router, publishers
from app.services.crypto import encryption_enabled

logger = logging.getLogger(__name__)

router = APIRouter(tags=["misc"])


@dataclass(frozen=True)
class _Probe:
    ok: bool
    #: What an anonymous caller is told — the exception class name and nothing
    #: else. See :func:`_short`.
    detail: str = ""
    #: The same failure with its message kept. Only ever rendered for a
    #: logged-in caller on ``/health/detail``; empty falls back to
    #: :attr:`detail`, which is what a monkeypatched probe supplies.
    verbose: str = ""

    def describe(self, *, public: bool) -> str:
        """The detail string this probe is allowed to show *this* caller."""
        return self.detail if public else (self.verbose or self.detail)


def _check_database(db: Session) -> _Probe:
    """Round-trip a trivial query.

    ``SELECT 1`` rather than merely holding a Session: the session factory does
    not connect until something is executed, so without a query this would
    report a dead Postgres as healthy.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return _Probe(False, _short(exc), _short(exc, public=False))
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
        return _Probe(False, _short(exc), _short(exc, public=False))
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


def _health_core(
    response: Response, db: Session, *, public: bool = True
) -> tuple[bool, DependencyOut, DependencyOut]:
    """Shared probe logic for both health endpoints.

    *public* decides how much of a failure the caller is shown. The probes
    always capture both renderings; anything else would mean the two endpoints
    could disagree about whether something is down while agreeing about why.
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

    db_out = DependencyOut(
        status="ok" if database.ok else "unavailable",
        required=True,
        detail=database.describe(public=public),
    )
    redis_out = DependencyOut(
        status="ok" if redis_probe.ok else "unavailable",
        required=redis_required,
        detail=redis_probe.describe(public=public),
    )
    return healthy, db_out, redis_out


#: A degraded health check answers with the same model as a healthy one, so its
#: 503 is documented as ``HealthOut`` rather than the usual error body. The
#: status code is the machine-readable half and the payload says which
#: dependency it was.
_DEGRADED = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": HealthOut,
        "description": "A required dependency is unreachable. Same body as a "
        "healthy response; `status` is `degraded` and the failing dependency "
        "says so.",
    }
}


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Liveness probe",
    responses={
        **_DEGRADED,
        **errors(status.HTTP_429_TOO_MANY_REQUESTS),
    },
)
@limiter.limit(settings.rate_limit_health)
def health(
    request: Request, response: Response, db: Session = Depends(get_db)
) -> HealthOut:
    """Public liveness probe.

    Caddy polls this as its ``health_uri``, so the status code is load-bearing.
    Returns only dependency reachability — no operational details.

    Limited despite that, and safely: Caddy's own poll originates from the proxy
    and carries no ``X-Forwarded-For``, so it counts against the proxy's address
    rather than any visitor's, at two requests a minute against a budget of
    sixty. Public callers arrive *through* Caddy and are bucketed per visitor by
    the rightmost forwarded entry, which is the one they cannot choose. The
    ``request`` parameter is not decoration — slowapi reads the limiter off it.
    """
    healthy, db_out, redis_out = _health_core(response, db)
    return HealthOut(status="ok" if healthy else "degraded", database=db_out, redis=redis_out)


@router.get(
    "/health/detail",
    response_model=HealthDetailOut,
    summary="Liveness probe with diagnostics",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            **_DEGRADED[status.HTTP_503_SERVICE_UNAVAILABLE],
            "model": HealthDetailOut,
        },
        **errors(*AUTHENTICATED),
    },
)
def health_detail(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HealthDetailOut:
    """Authenticated detail view — answers "why isn't X working?".

    LLM provider config, circuit-breaker state, platform list, and encryption
    status are only visible to logged-in users — and so is the *reason* a
    dependency is down. The public probe reports ``OperationalError`` and stops
    there, which is the right answer for an anonymous caller and a useless one
    for the person trying to fix it; here the message comes with it.
    """
    healthy, db_out, redis_out = _health_core(response, db, public=False)
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
