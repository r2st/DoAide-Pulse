"""Seed a fresh database with an account and the projects Pulse promotes.

Pulse promoting Pulse is the point: the first thing you see after signing in
is a real project you can generate a post about, rather than an empty state and
a form. The same registry carries the other products in the estate — a project
row with a ``repo_url`` is all the autopilot needs to start watching a repo (see
``app.tasks.autopilot_tasks.scan_all_projects``), so "promote GSTBot" is a row
here rather than a code change anywhere else.

Idempotent — running it twice changes nothing, and a project added to
``SEED_PROJECTS`` later is picked up by the next run without disturbing the ones
already there. Run with:

    python -m app.seed

``SEED_EMAIL`` must match the account that already owns the existing projects
when re-running against a live database: projects hang off a user, and a
different email would create a second account with the new projects on it.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.content import ContentIdea, ContentType
from app.models.project import AutopilotMode, Project, Tone, slugify
from app.models.user import User
from app.security import hash_password
from app.services import accounts

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

# Not a `.local` address: that TLD is special-use, and `EmailStr` rejects it —
# the account would seed fine and then 500 the moment /auth/me serialized it.
DEFAULT_EMAIL = "dev@pulse.example.com"


@dataclass(frozen=True)
class ProjectSpec:
    """One project to register, and the ideas that ship with it.

    Everything here maps onto a ``Project`` column except ``ideas``, which
    become ``ContentIdea`` rows so the projects page has something in it before
    any model has been called.
    """

    name: str
    description: str
    repo_url: str
    live_url: str | None
    tech_stack: list[str]
    target_audience: str
    keywords: list[str]
    tone: Tone
    #: Off for Pulse itself, draft for the rest. Never ``AUTO`` from a seed:
    #: a fresh install publishing to a real account unattended is a nasty
    #: surprise, and the flip to auto belongs to whoever is watching the queue.
    autopilot_mode: AutopilotMode
    ideas: list[tuple[ContentType, str, str]] = field(default_factory=list)


PULSE_SEED = ProjectSpec(
    name="Pulse",
    description=(
        "Pulse is an AI-powered marketing automation tool for developers who "
        "ship more than they write about. It watches your project repos, drafts "
        "blog posts and social copy when something meaningful lands, runs them "
        "past you, and publishes to Dev.to, Medium and the rest on a schedule — "
        "then tracks which pieces actually got read."
    ),
    repo_url="https://github.com/r2st/DoAide-Pulse",
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
        "Indie developers and small teams who ship side projects and want them "
        "promoted without hiring a marketer."
    ),
    keywords=[
        "marketing automation",
        "developer marketing",
        "ai content generation",
        "devto api",
        "content calendar",
    ],
    tone=Tone.TECHNICAL,
    # Off by default even for the seed: the autopilot publishing to a real
    # account on first boot would be a nasty surprise.
    autopilot_mode=AutopilotMode.OFF,
    ideas=[
        (
            ContentType.HOW_TO,
            "Automate your project's blog posts with Pulse",
            "The core use case, and the one people search for.",
        ),
        (
            ContentType.FEATURE_SPOTLIGHT,
            "How Pulse keeps one flaky LLM from breaking your content pipeline",
            "The provider chain and circuit breaker are genuinely unusual — worth "
            "a post of their own.",
        ),
        (
            ContentType.COMPARISON,
            "Pulse vs Buffer vs writing it yourself",
            "Comparison posts convert, and the honest answer here is interesting.",
        ),
    ],
)

# Marketing tone rather than technical for the two below, and deliberately: the
# reader is a business owner or a practising CA, not the developer who built it.
# The repo is still the signal; the voice is not the repo's.
GSTBOT = ProjectSpec(
    name="GSTBot",
    description=(
        "GSTBot is an AI-powered GST compliance platform for Indian SMBs. It "
        "reads invoices out of photos, PDFs and purchase registers, reconciles "
        "them against GSTR-2B to find the Input Tax Credit a manual match "
        "misses, scores suppliers on their filing history, and prepares GSTR-1 "
        "and GSTR-3B in a format the GST portal accepts — with deadline "
        "reminders so a quarter never closes by surprise."
    ),
    repo_url="https://github.com/r2st/GSTBot",
    live_url="https://gstbot.doaide.com",
    tech_stack=[
        "FastAPI",
        "SQLAlchemy",
        "PostgreSQL",
        "Celery",
        "Redis",
        "React",
        "Vite",
        "Tesseract OCR",
    ],
    target_audience=(
        "Owners and accountants at India's GST-registered small and mid-sized "
        "businesses, who reconcile invoices by hand and cannot justify ClearTax "
        "Pro pricing."
    ),
    keywords=[
        "gst reconciliation software",
        "gstr-2b matching",
        "input tax credit automation",
        "gstin validation",
        "gst return filing software",
        "invoice ocr india",
    ],
    tone=Tone.MARKETING,
    autopilot_mode=AutopilotMode.DRAFT,
    ideas=[
        (
            ContentType.HOW_TO,
            "How to reconcile GSTR-2B against your purchase register without "
            "opening Excel",
            "The reconciliation pain is the reason people go looking, and this is "
            "the query they type.",
        ),
        (
            ContentType.FEATURE_SPOTLIGHT,
            "Supplier health scores: spotting the vendor who will cost you ITC "
            "before they do",
            "The scoring is the feature competitors at this price do not have.",
        ),
        (
            ContentType.COMPARISON,
            "GSTBot vs ClearTax vs doing it in a spreadsheet",
            "Comparison against the incumbent is how a cheaper tool gets found.",
        ),
    ],
)

CAFLOW = ProjectSpec(
    name="CAFlow",
    description=(
        "CAFlow is an AI practice management platform for Chartered "
        "Accountants. It keeps the statutory compliance calendar, client records "
        "and document intake a firm runs on in one place — categorising incoming "
        "documents, tracking every filing deadline per client, chasing the "
        "paperwork with automated reminders, and raising the invoice at the end "
        "— plus a passwordless portal the firm's own clients use to check "
        "filing status and send documents in."
    ),
    repo_url="https://github.com/r2st/CAFlow",
    live_url="https://caflow.doaide.com",
    tech_stack=[
        "FastAPI",
        "SQLAlchemy",
        "PostgreSQL",
        "Celery",
        "Redis",
        "React",
        "Vite",
        "nginx",
    ],
    target_audience=(
        "Small and mid-sized Chartered Accountant firms in India running client "
        "compliance on spreadsheets, WhatsApp reminders and memory."
    ),
    keywords=[
        "ca practice management software",
        "compliance calendar for ca firms",
        "client document management ca",
        "gst filing reminders",
        "chartered accountant billing software",
        "ca client portal",
    ],
    tone=Tone.MARKETING,
    autopilot_mode=AutopilotMode.DRAFT,
    ideas=[
        (
            ContentType.HOW_TO,
            "Run a CA firm's compliance calendar without a single spreadsheet",
            "The spreadsheet is the incumbent, and naming it is what makes the "
            "post land.",
        ),
        (
            ContentType.FEATURE_SPOTLIGHT,
            "A client portal your clients will actually use: no passwords, no "
            "app to install",
            "Passwordless intake is the differentiator, and the objection it "
            "answers is real.",
        ),
        (
            ContentType.HOW_TO,
            "Stop chasing clients for documents: automated reminders that stay "
            "polite",
            "Document chasing is the daily complaint in every CA forum.",
        ),
    ],
)

#: Everything the seed registers, in the order it appears on the projects page.
SEED_PROJECTS: list[ProjectSpec] = [PULSE_SEED, GSTBOT, CAFLOW]


def _create_project(db: Session, user_id: int, spec: ProjectSpec) -> Project:
    """Insert *spec* and its ideas for *user_id*."""
    project = Project(
        user_id=user_id,
        name=spec.name,
        slug=slugify(spec.name),
        description=spec.description,
        repo_url=spec.repo_url,
        live_url=spec.live_url,
        tech_stack=list(spec.tech_stack),
        target_audience=spec.target_audience,
        keywords=list(spec.keywords),
        tone=spec.tone,
        autopilot_mode=spec.autopilot_mode,
        # Empty means "draft only" — a project queued for no platform cannot
        # publish itself even if the mode is later moved to auto by accident.
        autopilot_platforms=[],
    )
    db.add(project)
    db.flush()
    logger.info("created project %s (id=%s)", spec.name, project.id)

    for content_type, headline, rationale in spec.ideas:
        db.add(
            ContentIdea(
                project_id=project.id,
                content_type=content_type,
                headline=headline,
                rationale=rationale,
                source={"kind": "seed"},
            )
        )
    if spec.ideas:
        logger.info("  seeded %d content ideas", len(spec.ideas))
    return project


#: Columns the seed will fill in on a project that already exists, but only
#: where the stored value is empty. Deliberately not every column: ``name``,
#: ``tone`` and ``autopilot_mode`` always hold a value, so "empty" cannot
#: distinguish a default from a choice, and re-asserting them would undo an edit.
_BACKFILL_COLUMNS = (
    "repo_url",
    "live_url",
    "description",
    "target_audience",
    "tech_stack",
    "keywords",
)


def _backfill(project: Project, spec: ProjectSpec) -> list[str]:
    """Fill in *project*'s empty fields from *spec*. Returns what was filled.

    "Leave it alone" was too strong a reading of idempotent. A project row
    created before its repo was known — or by any path other than this one —
    kept a NULL ``repo_url`` through every subsequent seed, and a NULL
    ``repo_url`` is precisely what excludes a project from the autopilot sweep.
    The row looked registered, the projects page looked right, and it silently
    never scanned.

    Only empty values are written, so anything a human has since typed, cleared
    or corrected survives untouched. A user who deletes the repo URL on purpose
    will see it come back on the next seed run; that is the one case this gets
    wrong, and it is a visible, one-field correction rather than a project that
    quietly does nothing.
    """
    filled: list[str] = []
    for column in _BACKFILL_COLUMNS:
        seeded = getattr(spec, column)
        if not seeded or getattr(project, column):
            continue
        setattr(project, column, list(seeded) if isinstance(seeded, list) else seeded)
        filled.append(column)
    return filled


def seed(email: str | None = None, password: str | None = None) -> None:
    """Create or top up the seed account, and print the password once.

    Idempotent: an existing account keeps its password and its projects, and
    only fields still empty are filled in. Arguments beat ``SEED_EMAIL`` /
    ``SEED_PASSWORD``, which beat a generated password — which is the default
    precisely so a hardcoded one cannot reach production through this path.
    """
    # Normalized, and matched case-insensitively below: this function's whole
    # contract is that running it twice tops the same account up rather than
    # building a second one, and a ``SEED_EMAIL`` retyped in another case broke
    # that silently — see :mod:`app.services.accounts`.
    email = accounts.normalize_email(email or os.environ.get("SEED_EMAIL", DEFAULT_EMAIL))
    # A generated password beats a hardcoded one that ends up in a public repo
    # and then in production. It is printed once, here.
    generated = password is None and "SEED_PASSWORD" not in os.environ
    password = password or os.environ.get("SEED_PASSWORD") or secrets.token_urlsafe(12)

    db = SessionLocal()
    try:
        user = accounts.find_by_email(db, email)
        if user is None:
            user = User(
                email=email,
                full_name="Pulse Developer",
                hashed_password=hash_password(password),
            )
            db.add(user)
            db.flush()
            logger.info("created user %s", user.id)
            if generated:
                print(f"  seed password: {password}  (shown once — save it)")  # noqa: T201 — stdout only, never logged
        else:
            logger.info("user %s already exists, leaving it alone", user.id)

        for spec in SEED_PROJECTS:
            # Matched on slug rather than name: the slug is what the unique
            # constraint covers, and a project the user has since renamed keeps
            # its slug and must not be seeded a second time.
            existing = db.scalar(
                select(Project).where(
                    Project.user_id == user.id, Project.slug == slugify(spec.name)
                )
            )
            if existing is None:
                _create_project(db, user.id, spec)
            else:
                filled = _backfill(existing, spec)
                if filled:
                    logger.info(
                        "project %s already exists; filled in %s",
                        spec.name,
                        ", ".join(filled),
                    )
                else:
                    logger.info("project %s already exists, leaving it alone", spec.name)

        db.commit()
        logger.info("seed complete")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    seed()
