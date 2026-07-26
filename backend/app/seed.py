"""Seed a fresh database with an account and Herald's own project record.

Herald promoting Herald is the point: the first thing you see after signing in
is a real project you can generate a post about, rather than an empty state and
a form.

Idempotent — running it twice changes nothing. Run with:

    python -m app.seed
"""
from __future__ import annotations

import logging
import os
import secrets

from sqlalchemy import select

from app.database import SessionLocal
from app.models.content import ContentIdea, ContentType
from app.models.project import AutopilotMode, Project, Tone, slugify
from app.models.user import User
from app.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

DEFAULT_EMAIL = "dev@herald.local"

HERALD_DESCRIPTION = (
    "Herald is an AI-powered marketing automation tool for developers who ship "
    "more than they write about. It watches your project repos, drafts blog "
    "posts and social copy when something meaningful lands, runs them past you, "
    "and publishes to Dev.to, Medium and the rest on a schedule — then tracks "
    "which pieces actually got read."
)

#: Ideas that ship with the seed, so the projects page has something in it
#: before any model has been called.
SEED_IDEAS = [
    (
        ContentType.HOW_TO,
        "Automate your project's blog posts with Herald",
        "The core use case, and the one people search for.",
    ),
    (
        ContentType.FEATURE_SPOTLIGHT,
        "How Herald keeps one flaky LLM from breaking your content pipeline",
        "The provider chain and circuit breaker are genuinely unusual — worth a "
        "post of their own.",
    ),
    (
        ContentType.COMPARISON,
        "Herald vs Buffer vs writing it yourself",
        "Comparison posts convert, and the honest answer here is interesting.",
    ),
]


def seed(email: str | None = None, password: str | None = None) -> None:
    email = email or os.environ.get("SEED_EMAIL", DEFAULT_EMAIL)
    # A generated password beats a hardcoded one that ends up in a public repo
    # and then in production. It is printed once, here.
    generated = password is None and "SEED_PASSWORD" not in os.environ
    password = password or os.environ.get("SEED_PASSWORD") or secrets.token_urlsafe(12)

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                full_name="Herald Developer",
                hashed_password=hash_password(password),
            )
            db.add(user)
            db.flush()
            logger.info("created user %s", email)
            if generated:
                logger.info("  password: %s  (shown once — save it)", password)
        else:
            logger.info("user %s already exists, leaving it alone", email)

        project = db.scalar(
            select(Project).where(Project.user_id == user.id, Project.slug == "herald")
        )
        if project is None:
            project = Project(
                user_id=user.id,
                name="Herald",
                slug=slugify("Herald"),
                description=HERALD_DESCRIPTION,
                repo_url="https://github.com/r2st/Herald",
                live_url=None,
                tech_stack=[
                    "FastAPI",
                    "SQLAlchemy",
                    "PostgreSQL",
                    "Celery",
                    "Redis",
                    "React",
                    "Vite",
                    "Tailwind CSS",
                ],
                target_audience=(
                    "Indie developers and small teams who ship side projects and "
                    "want them promoted without hiring a marketer."
                ),
                keywords=[
                    "marketing automation",
                    "developer marketing",
                    "ai content generation",
                    "devto api",
                    "content calendar",
                ],
                tone=Tone.TECHNICAL,
                # Off by default even for the seed: the autopilot publishing to
                # a real account on first boot would be a nasty surprise.
                autopilot_mode=AutopilotMode.OFF,
                autopilot_platforms=[],
            )
            db.add(project)
            db.flush()
            logger.info("created project Herald (id=%s)", project.id)

            for content_type, headline, rationale in SEED_IDEAS:
                db.add(
                    ContentIdea(
                        project_id=project.id,
                        content_type=content_type,
                        headline=headline,
                        rationale=rationale,
                        source={"kind": "seed"},
                    )
                )
            logger.info("seeded %d content ideas", len(SEED_IDEAS))
        else:
            logger.info("project Herald already exists, leaving it alone")

        db.commit()
        logger.info("seed complete")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    seed()
