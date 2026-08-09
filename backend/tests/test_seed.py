"""The seed registry: what a fresh install (and a re-run) ends up with.

The autopilot only ever looks at active projects that have a ``repo_url``
(``scan_all_projects``), so "is GSTBot being promoted?" is really two
questions — did the row get created, and does its URL resolve to an
``owner/repo`` the GitHub client can call. Both are asserted here.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import seed as seed_module
from app.models.content import ContentIdea
from app.models.project import AutopilotMode, Project
from app.models.user import User
from tests.conftest import TestSession

SEED_EMAIL = "seed-test@herald.example.com"


@pytest.fixture
def run_seed(db, monkeypatch):
    """Run the real seed against the test database."""
    monkeypatch.setattr(seed_module, "SessionLocal", TestSession)

    def _run() -> None:
        seed_module.seed(email=SEED_EMAIL, password="seed-password-not-real")

    return _run


def _projects(db) -> dict[str, Project]:
    return {p.slug: p for p in db.scalars(select(Project))}


def test_seed_registers_every_spec(db, run_seed):
    run_seed()
    projects = _projects(db)
    assert set(projects) == {"herald", "gstbot", "caflow"}


@pytest.mark.parametrize(
    ("slug", "repo"),
    [
        ("herald", "r2st/Herald"),
        ("gstbot", "r2st/GSTBot"),
        ("caflow", "r2st/CAFlow"),
    ],
)
def test_seeded_projects_are_scannable(db, run_seed, slug, repo):
    """Active, and with a repo the autopilot can actually resolve."""
    run_seed()
    project = _projects(db)[slug]
    assert project.is_active is True
    assert project.repo_full_name == repo


def test_new_products_draft_rather_than_publish(db, run_seed):
    """Nothing seeded may publish unattended — see ``ProjectSpec``."""
    run_seed()
    projects = _projects(db)
    assert projects["gstbot"].autopilot_mode is AutopilotMode.DRAFT
    assert projects["caflow"].autopilot_mode is AutopilotMode.DRAFT
    assert all(p.autopilot_platforms == [] for p in projects.values())
    assert all(p.autopilot_mode is not AutopilotMode.AUTO for p in projects.values())


def test_seed_is_idempotent(db, run_seed):
    """A second run adds no user, no project and no duplicate ideas."""
    run_seed()
    ideas_after_first = db.scalar(select(ContentIdea).order_by(ContentIdea.id))
    counts = (
        len(list(db.scalars(select(User)))),
        len(list(db.scalars(select(Project)))),
        len(list(db.scalars(select(ContentIdea)))),
    )

    run_seed()
    db.expire_all()
    assert (
        len(list(db.scalars(select(User)))),
        len(list(db.scalars(select(Project)))),
        len(list(db.scalars(select(ContentIdea)))),
    ) == counts
    assert db.scalar(select(ContentIdea).order_by(ContentIdea.id)).id == ideas_after_first.id


def test_seed_adds_new_specs_to_an_existing_account(db, run_seed):
    """The path a live database takes: user and Herald exist, the rest do not."""
    run_seed()
    db.expire_all()
    for slug in ("gstbot", "caflow"):
        db.delete(_projects(db)[slug])
    db.commit()

    run_seed()
    db.expire_all()
    assert set(_projects(db)) == {"herald", "gstbot", "caflow"}
    assert len(list(db.scalars(select(User)))) == 1


def test_seed_fills_in_a_repo_url_that_was_never_set(db, run_seed):
    """The reason a project can sit registered and never scan.

    A NULL ``repo_url`` is what excludes a project from ``scan_all_projects``,
    and "already exists, leaving it alone" meant a row that arrived without one
    kept the NULL through every later seed — registered, visible, and silently
    outside the sweep forever.
    """
    run_seed()
    db.expire_all()
    project = _projects(db)["gstbot"]
    project.repo_url = None
    db.commit()

    run_seed()
    db.expire_all()

    project = _projects(db)["gstbot"]
    assert project.repo_full_name == "r2st/GSTBot"
    assert project.autopilot_blocked_reason is None


def test_seed_does_not_overwrite_what_somebody_typed(db, run_seed):
    """Backfill fills gaps; it does not re-assert the registry over an edit."""
    run_seed()
    db.expire_all()
    project = _projects(db)["caflow"]
    project.repo_url = "https://github.com/someone-else/CAFlow"
    project.description = "Rewritten by hand."
    project.keywords = ["only this one"]
    db.commit()

    run_seed()
    db.expire_all()

    project = _projects(db)["caflow"]
    assert project.repo_url == "https://github.com/someone-else/CAFlow"
    assert project.description == "Rewritten by hand."
    assert project.keywords == ["only this one"]
