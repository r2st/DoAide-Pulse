"""Test fixtures.

Everything runs against an in-memory SQLite database with no external services.
The LLM chain, GitHub and every platform API are unreachable in tests by design
— which means the fallback paths are what get exercised, and those are exactly
the paths that matter when a free-tier provider has a bad afternoon.
"""
from __future__ import annotations

import os

# Must be set before anything imports app.config, which reads it at import time.
# Use direct assignment (not setdefault) so the production .env file on the
# server cannot leak into the test suite via pydantic-settings.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "test-secret-not-a-real-one"
os.environ["CELERY_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "development"
# Registration is closed in production; the tests that exercise the happy path
# open it explicitly (see tests/test_auth.py), so it must be reachable here.
os.environ["REGISTRATION_ENABLED"] = "true"
os.environ["REGISTRATION_INVITE_TOKEN"] = ""
# No provider keys: the chain is empty, so generation takes the template path.
for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"):
    os.environ[key] = ""
# Assigned, not `setdefault`: pydantic-settings falls through to the repo's own
# ``.env`` for anything the environment leaves unset, so on a developer machine
# with real SMTP credentials on disk the mailer tests would find themselves
# configured and assert against the wrong branch.
for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ[key] = ""
# GitHub token must be blank in tests — the production .env has a real one and
# pydantic-settings reads it.
os.environ["GITHUB_TOKEN"] = ""
# CORS must be the dev defaults, not the production origin.
os.environ["BACKEND_CORS_ORIGINS"] = "http://localhost:5173,http://localhost:3000"

# Clear the settings cache and rebuild the module-level singleton so test
# environment variables take effect even when running on a server whose .env
# holds production values.
import app.config as _cfg  # noqa: E402

_cfg.get_settings.cache_clear()
_cfg.settings = _cfg.get_settings()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import ratelimit  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.project import Project, Tone  # noqa: E402
from app.models.user import User  # noqa: E402
from app.security import hash_password  # noqa: E402

# StaticPool keeps every connection pointed at the same in-memory database —
# without it each connection gets its own empty one.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Empty every rate-limit counter around each test.

    The limiter is a module-level singleton with in-process storage, so without
    this the ``auth`` fixture's one login per test accumulates and the 30th test
    to ask for a token gets a 429 instead.
    """
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def user(db) -> User:
    row = User(
        email="dev@example.com",
        full_name="Dev",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def auth(client, user) -> dict[str, str]:
    """Authorization header for ``user``."""
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "hunter2hunter2"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def sql_log():
    """Every statement the engine executes while the fixture is active.

    For pinning down N+1s: the only thing that separates an eager load from a
    lazy one is how many SELECTs come out, so that is what gets asserted.
    """
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)


@pytest.fixture
def project(db, user) -> Project:
    row = Project(
        user_id=user.id,
        name="Herald",
        slug="herald",
        description="AI marketing automation for developer projects.",
        repo_url="https://github.com/r2st/Herald",
        live_url="https://herald.example.com",
        tech_stack=["FastAPI", "React"],
        target_audience="Indie developers",
        keywords=["marketing automation", "developer marketing"],
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
