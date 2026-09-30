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
from app.services.publishers import breaker as publishers_breaker

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


def _check_workers() -> _Probe | None:
    """Whether any Celery worker is actually consuming, or ``None`` if none should be.

    Redis answering ``PING`` is not the same fact. The broker is a queue: it
    accepts everything whether or not anything is draining it, so a worker that
    was OOM-killed or never came back from a deploy leaves every probe here
    green while nothing publishes, no repo is scanned, no trigger fires and no
    webhook is delivered — Herald's entire product runs in that process.

    This is deliberately *not* on the public ``/health``. Two reasons, and each
    would be enough:

    * Caddy polls that endpoint as its ``health_uri``, so its status code
      decides whether the API stays in rotation. A dead worker does not stop
      the API serving requests, and taking the site down over one would turn a
      background outage into a total one — including the UI somebody needs to
      find out what happened.
    * This probe is a *broadcast* over the broker with a wait attached, unlike
      the two local round-trips beside it. On an anonymous endpoint that
      answers sixty times a minute per caller, that is an amplifier pointed at
      the queue the workers are trying to read.

    ``None`` when ``celery_enabled`` is off: tasks run inline in the API
    process, so there is no worker to look for and "unavailable" would be a
    false alarm about the intended configuration.
    """
    if not settings.celery_enabled:
        return None
    try:
        from app.tasks.celery_app import celery_app

        # Bounded by the same timeout as the other probes. With nothing
        # listening this waits it out in full, which is the cost of the answer.
        replies = celery_app.control.ping(timeout=settings.health_check_timeout_seconds)
    except Exception as exc:  # kombu raises a family of unrelated errors
        return _Probe(False, _short(exc), _short(exc, public=False))
    if not replies:
        detail = "no worker answered"
        return _Probe(False, detail, detail)
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
    workers = _check_workers()
    if workers is not None and not workers.ok:
        # Logged as well as reported, which every other probe in this file
        # already was: ``_health_core`` writes a line whenever the database or
        # Redis is down, and this was the one failure that existed only in a
        # response body. It is also the failure with the least else to find it —
        # the worker fleet being gone means no task writes a line either, so a
        # journal covering the whole outage held nothing about it at all, and the
        # one moment Herald *knew* went unrecorded.
        #
        # Not folded into ``healthy`` (see below), so this is deliberately not
        # the 503 branch's line: an operator grepping for why publishing stopped
        # needs the fact, not a status code it must not change.
        logger.warning("worker probe failed: %s", workers.verbose or workers.detail)
    # Never folded into `healthy`: a dead worker is a real outage and not this
    # endpoint's kind of one — see `_check_workers`. It is reported so the
    # person asking "why has nothing published?" is told, rather than shown
    # three green dependencies and left to guess.
    workers_out = DependencyOut(
        status="disabled" if workers is None else ("ok" if workers.ok else "unavailable"),
        required=False,
        detail="" if workers is None else workers.describe(public=False),
    )
    return HealthDetailOut(
        status="ok" if healthy else "degraded",
        database=db_out,
        redis=redis_out,
        workers=workers_out,
        llm_providers=llm_router.configured_providers(),
        llm_breakers_open=llm_router.breaker.snapshot(),
        publish_breakers_open=publishers_breaker.snapshot(),
        github_configured=bool(settings.github_token),
        credential_encryption=encryption_enabled(),
        implemented_platforms=[p.value for p in publishers.implemented_platforms()],
    )
