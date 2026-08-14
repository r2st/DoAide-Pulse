"""Herald FastAPI application entrypoint."""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm.exc import StaleDataError
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class BodySizeLimitMiddleware:
    """Reject oversized request bodies, declared or not.

    Two halves, because a body arrives in two ways:

    * **Declared.** ``Content-Length`` is checked before the route runs, so an
      oversized upload is refused without being read. This is the cheap half and
      the one that answers honestly — the client learns the body was too big
      rather than watching the connection go quiet.
    * **Undeclared.** HTTP/1.1 lets a client send ``Transfer-Encoding: chunked``
      with no ``Content-Length`` at all, and a guard that only reads the header
      waves those through: the route reaches for ``await request.json()``, and
      the server buffers however many gigabytes are on the way. So the receive
      channel is counted as it is consumed and cut off at the same cap.

    Raw ASGI rather than ``BaseHTTPMiddleware`` because this one has to sit
    between the app and its receive channel, which is the one thing the dispatch
    interface does not hand over.

    Once the cap is passed the channel reports a disconnect instead of more
    body. The app unwinds on its own from there — Starlette raises
    ``ClientDisconnect`` out of the stream and FastAPI renders it as a 400 —
    and that answer is discarded in favour of the 413 the situation actually
    calls for. Discarding rather than raising on purpose: an exception thrown
    from inside ``receive`` gets caught by whatever happens to be reading the
    body (FastAPI catches everything there and calls it a parse error) or
    arrives wrapped in a task group's exception group from the middleware above.
    A flag crosses those boundaries unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared:
            try:
                length = int(declared)
            except (ValueError, OverflowError):
                length = -1
            if length < 0:
                await _respond(scope, send, 400, "Invalid Content-Length header")
                return
            if length > MAX_BODY_BYTES:
                await _respond(scope, send, 413, "Request body too large")
                return

        read = 0
        over = False
        started = False

        async def _counted_receive() -> Message:
            nonlocal read, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                read += len(message.get("body", b""))
                if read > MAX_BODY_BYTES:
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def _guarded_send(message: Message) -> None:
            nonlocal started
            # Whatever the app made of the truncated body, it is answering a
            # question it was never given the whole of. Drop it.
            if over:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, _counted_receive, _guarded_send)

        if over and not started:
            await _respond(scope, send, 413, "Request body too large")
        elif over:
            # The route answered and then went back for more body. Its response
            # is already on the wire and cannot be taken back, so the cap has
            # done the half that mattered — the read stopped — and there is no
            # second answer to send.
            logger.warning("body cap hit after the response had started")


async def _respond(scope: Scope, send: Send, code: int, detail: str) -> None:
    await JSONResponse({"detail": detail}, status_code=code)(scope, _no_body, send)


async def _no_body() -> Message:
    """A receive channel for a response that will not read the request.

    ``Response.__call__`` takes one and never calls it for a plain body, but the
    signature requires something, and something that returns a disconnect is the
    honest filler: by the time either caller gets here, the request body is
    either unread by choice or already over the cap.
    """
    return {"type": "http.disconnect"}


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

    The startup line exists because the connection budget is the setting most
    likely to be wrong and the least likely to announce it: too low and the app
    is slow under load for no visible reason, too high and it is the fourth
    process that cannot connect at all, in a log line belonging to some other
    service. It is per process, so the number that matters is this one times
    however many processes the unit file starts — which is why it is logged
    where an operator counting them will see it once per process.

    On shutdown, dispose the pool so open connections are returned to PostgreSQL
    rather than left for it to time out.
    """
    from app.database import engine

    logger.info(
        "serving with a connection budget of %d+%d per process "
        "(pool_timeout=%.1fs, statement_timeout=%.1fs)",
        settings.db_pool_size,
        settings.db_max_overflow,
        settings.db_pool_timeout_seconds,
        settings.db_statement_timeout_seconds,
    )
    yield
    engine.dispose()
    logger.info("database pool disposed — shutting down")


def create_app() -> FastAPI:
    """Build the FastAPI application: logging, middleware, routers, lifespan.

    A factory rather than a module-level singleton so tests can build an app
    against settings they have just changed. Everything with an ordering
    constraint is done here in the order it has to happen — see the comments
    below, starting with logging, which must come before the first logger.
    """
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

    # A row was written by somebody else between this request loading it and
    # committing. SQLAlchemy raises this from ``session.commit()`` for any
    # mapper with a ``version_id_col`` — ``Content`` is the one that has one, and
    # its version column is there precisely so the second writer finds out.
    #
    # Handled here rather than at each write path because there is no write path
    # this cannot happen on: the router's PATCH, the passage editor, the
    # publisher settling a queue row and the autopilot all commit a ``Content``,
    # and any of them can lose the race. Left to the catch-all above it would be
    # a 500 — "something failed inside Herald", quote the request id — for a
    # perfectly ordinary outcome the caller can act on by reloading.
    #
    # 409 rather than 412: no precondition was sent, so none failed. The
    # resource simply moved underneath a request that was already in flight,
    # which is what 409 is for.
    async def _stale_data(request: Request, exc: Exception) -> JSONResponse:
        logger.info(
            "stale write refused [request_id=%s]: %s",
            getattr(request.state, "request_id", "unknown"),
            exc,
        )
        return JSONResponse(
            {
                "detail": "Somebody else changed this while you were editing it. "
                "Reload and try again."
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    app.add_exception_handler(StaleDataError, _stale_data)

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
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "If-Match"],
        # ETag is here because the SPA is a cross-origin caller in development
        # and a browser hides every response header not on this list. It carries
        # the content version the editor echoes back as If-Match; without it the
        # header would be present on the wire and invisible to the code that
        # needs it. If-Match joins allow_headers for the same reason in reverse.
        expose_headers=["X-Total-Count", "X-Request-ID", "ETag"],
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
            status.HTTP_413_CONTENT_TOO_LARGE,
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
