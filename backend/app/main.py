"""Herald FastAPI application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.ratelimit import limiter, rate_limit_exceeded_handler
from app.routers import analytics, auth, calendar, content, misc, projects
from app.routers import settings as settings_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI-powered marketing automation for developer projects.",
        debug=settings.debug,
    )

    # slowapi's decorators read the limiter off application state at request
    # time, so this assignment is what makes @limiter.limit(...) live.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    prefix = settings.api_v1_prefix
    app.include_router(misc.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(projects.router, prefix=prefix)
    app.include_router(content.router, prefix=prefix)
    app.include_router(calendar.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)
    app.include_router(settings_router.router, prefix=prefix)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"app": settings.app_name, "docs": "/docs", "health": f"{prefix}/health"}

    return app


app = create_app()
