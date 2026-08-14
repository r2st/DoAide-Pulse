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
# Long enough to clear app.config._MIN_JWT_SECRET_BYTES. The old 26-byte value
# was accepted here only because the length floor is a production-only rule —
# but PyJWT warns on every `decode()` with a key shorter than the SHA-256 output
# (InsecureKeyLengthWarning, RFC 7518 §3.2), and nearly every test in the suite
# decodes a token. That was ~700 warnings a run, all of them noise, and noise on
# that scale is where a real warning goes to hide. The fixture now signs with a
# key the size production demands. Pinned by tests/test_jwt_secret_strength.py.
os.environ["JWT_SECRET"] = "test-secret-not-a-real-one-but-long-enough"
os.environ["CELERY_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "development"
# Registration is closed in production; the tests that exercise the happy path
# open it explicitly (see tests/test_auth.py), so it must be reachable here.
os.environ["REGISTRATION_ENABLED"] = "true"
os.environ["REGISTRATION_INVITE_TOKEN"] = ""
# bcrypt's minimum work factor. The default 12 costs ~230ms per hash and the
# fixtures below make a user for nearly every test in the suite, which came to
# over half the total runtime — long enough that a full run reads as a hang and
# gets killed instead of waited out. At 4 the same code path costs ~1ms. The
# production floor is enforced in app.config, so this cannot escape the suite.
# Unset again once the singleton is built, a few lines below.
os.environ["BCRYPT_ROUNDS"] = "4"
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
# Rate limit storage must be in-memory for tests. The production .env points
# at Redis, and swallow_errors=True silently lets everything through when the
# storage backend is unreachable — which hides real rate-limit bugs.
os.environ["RATE_LIMIT_STORAGE_URI"] = ""
os.environ["RATE_LIMIT_ENABLED"] = "true"

# Clear the settings cache and rebuild the module-level singleton so test
# environment variables take effect even when running on a server whose .env
# holds production values.
import app.config as _cfg  # noqa: E402

_cfg.get_settings.cache_clear()
_cfg.settings = _cfg.get_settings()

# The singleton now holds the cheap work factor, which is all the suite wanted.
# Leaving BCRYPT_ROUNDS in the environment would go further than that: every
# `Settings(environment="production", ...)` a test builds to exercise a
# production rule reads os.environ too, even with `_env_file=None`, so each one
# would inherit 4 and trip the production floor for reasons having nothing to
# do with what it was written to check. Unsetting it here keeps the override
# where it belongs — on the app's own settings object — instead of leaving a
# trap for the next test that constructs a production config.
del os.environ["BCRYPT_ROUNDS"]

import pytest  # noqa: E402

# Rebuild the limiter with the test settings. If any module imported
# ``app.ratelimit`` before conftest ran (transitive import from app.main during
# pytest collection), the limiter was created with the production .env values
# (e.g. Redis storage URI). Replacing it here guarantees in-memory storage.
import slowapi  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import ratelimit  # noqa: E402

ratelimit.limiter = slowapi.Limiter(
    key_func=ratelimit.client_key,
    storage_uri=_cfg.settings.rate_limit_storage_uri or "memory://",
    enabled=_cfg.settings.rate_limit_enabled,
    headers_enabled=True,
    swallow_errors=True,
    key_prefix="herald",
)

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.project import Project, Tone  # noqa: E402
from app.models.user import User  # noqa: E402
from app.security import hash_password  # noqa: E402

# Wire the rebuilt limiter into the running app. ``create_app()`` already set
# ``app.state.limiter``, but if the module was loaded before conftest's rebuild
# it would be the stale one. Overwriting it guarantees the test-time limiter
# is what slowapi's decorators dispatch through.
app.state.limiter = ratelimit.limiter

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
def connect(db, user):
    """Give the account a live connection to one or more platforms.

    The autopilot only queues destinations the owner has actually connected —
    see ``content_pipeline._publishable_destinations``, which drops the rest
    rather than queueing a publication that can only fail terminally and take
    the piece to ``failed`` with it. So a test about *routing* has to say which
    platforms are connected, or the routing it means to exercise does not
    happen and the piece quietly goes to review instead.

    Takes platform values or members, and may be called more than once.
    """
    from app.models.platform_connection import ConnectionStatus, PlatformConnection
    from app.models.publication import Platform
    from app.services.crypto import encrypt_credentials

    def _connect(*platforms) -> None:
        for raw in platforms:
            platform = raw if isinstance(raw, Platform) else Platform(raw)
            db.add(
                PlatformConnection(
                    user_id=user.id,
                    platform=platform,
                    status=ConnectionStatus.CONNECTED,
                    encrypted_credentials=encrypt_credentials({"api_key": "k"}),
                    display_name=f"@herald-{platform.value}",
                )
            )
        db.commit()

    return _connect


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
def task_session(db):
    """A ``SessionLocal`` stand-in that hands a background task the test session.

    Celery tasks open their own session and close it in a ``finally``. Handing
    them the test's session directly means the first task to finish closes the
    session the assertions are about, so the proxy swallows ``close()`` — the
    fixture owns this session's lifetime, not the task.

    Returns the factory, ready to be patched over a module's ``SessionLocal``.
    """

    class _NoCloseProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    return lambda: _NoCloseProxy()


def repo_activity(*, commits: int = 0, release: bool = False, head: str = "abc123"):
    """Fabricated GitHub activity, so the tests are about policy not HTTP."""
    from datetime import UTC, datetime

    from app.services.github_client import Commit, Release, RepoActivity

    return RepoActivity(
        full_name="r2st/Herald",
        new_commits=[
            Commit(
                sha=f"sha{i}",
                message=f"feat: thing {i}\n\nbody",
                author="r2st",
                committed_at=datetime(2026, 7, 1, tzinfo=UTC),
                url="https://github.com/r2st/Herald/commit/x",
            )
            for i in range(commits)
        ],
        new_release=(
            Release(
                tag="v1.2.0",
                name="Calendar drag-and-drop",
                body="- Drag to reschedule\n- SEO panel",
                published_at=datetime(2026, 7, 20, tzinfo=UTC),
                url="https://github.com/r2st/Herald/releases/v1.2.0",
                prerelease=False,
            )
            if release
            else None
        ),
        head_sha=head,
        latest_tag="v1.2.0" if release else None,
    )


@pytest.fixture
def stub_github(monkeypatch, task_session):
    """Replace the GitHub fetch with a fixture, and record what it was asked.

    Lives here rather than in one test module because three of them need it, and
    importing a fixture across modules shadows it into an F811 at every use.
    """
    from app.tasks import autopilot_tasks

    calls: list[dict] = []
    activity = {"value": repo_activity()}

    def fake_fetch(full_name, *, since_sha=None, since_tag=None):
        calls.append({"full_name": full_name, "since_sha": since_sha})
        return activity["value"]

    monkeypatch.setattr(autopilot_tasks.github_client, "fetch_activity", fake_fetch)
    monkeypatch.setattr(autopilot_tasks, "SessionLocal", task_session)
    return {"calls": calls, "set": lambda a: activity.update(value=a)}


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
