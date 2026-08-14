"""Herald FastAPI application entrypoint."""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.logging_config import configure_logging, request_id_var
from app.ratelimit import limiter, rate_limit_exceeded_handler
from app.routers import (
    analytics,
    auth,
    calendar,
    content,
    misc,
    projects,
    templates,
    triggers,
    webhooks,
)
from app.routers import settings as settings_router
from app.schemas.errors import errors
from app.schemas.settings import RootOut

logger = logging.getLogger(__name__)

#: Reject request bodies larger than 1 MB. The largest legitimate payload is a
#: content body (~200 KB max via schema validation), and this gives comfortable
#: headroom while stopping a multi-GB upload from consuming all memory.
MAX_BODY_BYTES = 1 * 1024 * 1024


def docs_enabled() -> bool:
    """Whether to publish /docs, /redoc and /openapi.json.

    On in development, off in production. The schema is a complete map of the
    API — every route, every field, every enum — and Herald's is served from the
    same origin as the SPA, so leaving it up hands an anonymous visitor the
    inventory of what to try. ``DEBUG=true`` overrides, for the case where
    production is what needs poking at.
    """
    return (not settings.is_production) or settings.debug


def traceback_responses_enabled() -> bool:
    """Whether Starlette may return a traceback page instead of a clean 500.

    ``DEBUG=true`` is a documented escape hatch for reopening the docs on a live
    box (see :func:`docs_enabled`), and an operator reaching for it is thinking
    about the schema, not about Starlette's error middleware. But the flag is
    also what ``ServerErrorMiddleware`` checks *before* consulting an installed
    handler:

        if self.debug:                 # <- traceback response, we never run
            response = self.debug_response(request, exc)
        elif self.handler is None: ...
        else: response = await self.handler(request, exc)

    So ``DEBUG=true`` silently takes the catch-all below out of circuit and
    serves every unhandled exception as a full traceback — source lines, local
    variables, the connection string in a DB error's frame — to whoever sent the
    request. The two uses of the flag are separable, so separate them: in
    production the docs override stays, the traceback override does not.
    """
    return settings.debug and not settings.is_production


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hook.

    On shutdown, dispose the SQLAlchemy engine pool so open connections are
    returned to PostgreSQL rather than orphaned.
    """
    yield
    from app.database import engine

    engine.dispose()
    logger.info("database pool disposed — shutting down")


def create_app() -> FastAPI:
    # Before anything else builds a logger or logs a line. Uvicorn configures
    # its own loggers and leaves the root alone, so without this every
    # ``logger.info`` in the tree goes to Python's WARNING-floored last resort
    # and is dropped — see ``app.logging_config``.
    configure_logging()
    docs = docs_enabled()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI-powered marketing automation for developer projects.",
        debug=traceback_responses_enabled(),
        lifespan=_lifespan,
        # None removes the route outright — a 404, not a 401. There is nothing
        # here worth an auth prompt, and a prompt confirms the schema exists.
        # FastAPI derives /docs and /redoc from openapi_url, so all three go.
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
    )

    # slowapi's decorators read the limiter off application state at request
    # time, so this assignment is what makes @limiter.limit(...) live.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Catch-all for unhandled exceptions. Without this, FastAPI returns the
    # exception message (and tracebacks in debug mode) to the caller — an
    # information leak that also looks unprofessional. See
    # traceback_responses_enabled() for why DEBUG must not reach Starlette here.
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception("unhandled exception [request_id=%s]", request_id)
        # Set the header here rather than leaving it to RequestIDMiddleware:
        # Starlette installs ServerErrorMiddleware *outside* the whole user
        # middleware stack, so a raising route unwinds past RequestIDMiddleware
        # before it can decorate a response, and this 500 would go out with no
        # id at all. The id is in the log line above either way — but a 500 the
        # caller cannot quote back is the one error where that matters most.
        return JSONResponse(
            {"detail": "Internal server error"},
            status_code=500,
            headers={"X-Request-ID": request_id},
        )

    app.add_exception_handler(Exception, _unhandled_exception)

    # --- Middleware stack (outermost first) ---

    # Request ID: every request gets a unique id for log correlation and
    # debugging. Returned in X-Request-ID so the frontend can quote it in
    # bug reports.
    class RequestIDMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
            request.state.request_id = request_id
            # Also on the logging context, so the id reaches log lines written
            # by code that has no idea a request exists — which is most of the
            # code that logs anything worth correlating. Reset on the way out:
            # BaseHTTPMiddleware runs each request in its own task and the
            # context is copied per task, but the token makes that a property of
            # this middleware rather than of Starlette's internals.
            token = request_id_var.set(request_id)
            try:
                response = await call_next(request)
            finally:
                request_id_var.reset(token)
            response.headers["X-Request-ID"] = request_id
            return response

    app.add_middleware(RequestIDMiddleware)

    # Body size guard: reject oversized payloads before they reach a route.
    class BodySizeLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            cl = request.headers.get("content-length")
            if cl:
                try:
                    length = int(cl)
                except (ValueError, OverflowError):
                    return JSONResponse(
                        {"detail": "Invalid Content-Length header"},
                        status_code=400,
                    )
                if length < 0:
                    return JSONResponse(
                        {"detail": "Invalid Content-Length header"},
                        status_code=400,
                    )
                if length > MAX_BODY_BYTES:
                    return JSONResponse(
                        {"detail": "Request body too large"},
                        status_code=413,
                    )
            return await call_next(request)

    app.add_middleware(BodySizeLimitMiddleware)

    # Security headers. Caddy already sets HSTS and some of these, but
    # defence-in-depth means the app should not rely on that.
    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=()"
            )
            # Prevent caches (shared proxies, browsers) from storing
            # authenticated responses that may carry tokens or user data.
            response.headers["Cache-Control"] = "no-store"
            # Basic CSP. 'unsafe-inline' is needed for Swagger UI's scripts
            # and styles; tightened to 'self' when docs are disabled.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'"
            )
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    # No allow_credentials: Herald authenticates with a bearer token the SPA
    # holds and sends explicitly, never with a cookie. Allowing credentialed
    # cross-origin requests would ask browsers to attach ambient credentials to
    # them — the precondition for CSRF — and buy nothing, since there are none
    # to attach. It also stops any origin in the list from ever reading a
    # response with the caller's session implicitly along for the ride.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Total-Count", "X-Request-ID"],
    )

    prefix = settings.api_v1_prefix
    app.include_router(misc.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(projects.router, prefix=prefix)
    app.include_router(content.router, prefix=prefix)
    app.include_router(calendar.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)
    app.include_router(settings_router.router, prefix=prefix)
    app.include_router(webhooks.router, prefix=prefix)
    app.include_router(triggers.router, prefix=prefix)
    app.include_router(templates.router, prefix=prefix)

    @app.get(
        "/",
        response_model=RootOut,
        # ``docs`` is absent rather than null where the schema is not served —
        # the field is optional in the model, and this is what keeps the
        # response from advertising it as an explicit nothing.
        response_model_exclude_none=True,
        summary="Where the API is",
        tags=["misc"],
        responses=errors(
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        ),
    )
    def root() -> RootOut:
        """Service name and the health path. Unauthenticated, and says nothing
        about who asked.

        The two failures are the body-size middleware's, which runs ahead of
        every route in the app including this one: a malformed
        ``Content-Length`` is a 400 and an oversized one a 413, neither of
        which this handler ever sees.
        """
        # Don't advertise a route that isn't there.
        return RootOut(
            app=settings.app_name,
            health=f"{prefix}/health",
            docs="/docs" if docs else None,
        )

    return app


app = create_app()
