"""The usage table is bounded by a purge, and the purge is actually called.

``llm_usage.purge`` was written by R79 and had no caller, and the setting its
model docstring named was not in ``config.py`` — so the one table in Herald
written on a path nothing rate-limits grew without limit. A row per completion
*attempt*, written by the autopilot on a schedule whether or not anybody asked
for a piece.

The wiring is what these tests are mostly about. A purge function that works
perfectly and is never scheduled is the state this replaced, and it is not
visible in a test of the function alone.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.llm_usage import LLMUsage
from app.models.mixins import utcnow
from app.services import llm_usage
from app.tasks import maintenance_tasks
from app.tasks.celery_app import celery_app


def _usage(db, *, age_days: float = 0.0) -> LLMUsage:
    row = LLMUsage(provider="groq", model="llama", ok=True, duration_ms=100)
    db.add(row)
    db.flush()
    if age_days:
        row.created_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# The wiring                                                                   #
# --------------------------------------------------------------------------- #


def test_the_purge_is_on_the_beat_schedule():
    """The gap this closes. A purge nothing calls is not a bound."""
    entry = celery_app.conf.beat_schedule["purge-old-llm-usage"]

    assert entry["task"] == "app.tasks.maintenance_tasks.purge_old_llm_usage"
    assert entry["schedule"] == 86400.0


def test_the_retention_setting_exists_and_is_positive():
    """Named by the model docstring long before it was in ``config``."""
    assert settings.llm_usage_retention_days > 0


def test_a_retention_of_zero_is_refused():
    """Zero is a cutoff of ``utcnow()`` — it deletes the rows as they land.

    Not a way to switch the purge off, and a config value that silently means
    "delete everything" is the kind of setting nobody knows is thrown.
    """
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(llm_usage_retention_days=0)


def test_the_task_reads_the_setting(db, monkeypatch):
    """Not a hardcoded window — the setting is what the operator can change."""
    monkeypatch.setattr(settings, "llm_usage_retention_days", 7)

    class _NoClose:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr(maintenance_tasks, "SessionLocal", lambda: _NoClose())

    _usage(db, age_days=10)
    kept = _usage(db, age_days=3)

    assert maintenance_tasks.purge_old_llm_usage() == {"purged": 1}
    assert [r.id for r in db.query(LLMUsage)] == [kept.id]


# --------------------------------------------------------------------------- #
# The function                                                                 #
# --------------------------------------------------------------------------- #


def test_rows_past_the_window_go_and_the_rest_stay(db):
    # Ids read before the delete: touching the attribute afterwards makes
    # SQLAlchemy refresh an instance whose row is gone.
    old_id = _usage(db, age_days=40).id
    kept_id = _usage(db, age_days=5).id

    assert llm_usage.purge(db, days=30) == 1

    remaining = [r.id for r in db.query(LLMUsage)]
    assert remaining == [kept_id]
    assert old_id not in remaining


def test_a_purge_with_nothing_to_do_reports_zero(db):
    _usage(db, age_days=1)

    assert llm_usage.purge(db, days=30) == 0
    assert db.query(LLMUsage).count() == 1


def test_the_purge_commits(db):
    """Returning a count off an uncommitted delete would be a lie to the caller."""
    _usage(db, age_days=40)
    llm_usage.purge(db, days=30)

    db.rollback()

    assert db.query(LLMUsage).count() == 0


def test_a_row_exactly_at_the_boundary_is_kept(db):
    """``<`` not ``<=``: the window is what the metrics endpoint reports on.

    A row on the edge is inside the window the endpoint would quote, and
    deleting it would make the two disagree about the same day.
    """
    row = _usage(db)
    row.created_at = utcnow() - timedelta(days=30) + timedelta(seconds=30)
    db.commit()

    assert llm_usage.purge(db, days=30) == 0


def test_the_purge_does_not_touch_other_tables(db, project):
    """A delete on a shared session with a broad filter is worth pinning."""
    from app.models.content import Content, ContentStatus, ContentType

    piece = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title="Still here",
        slug="still-here",
        status=ContentStatus.DRAFT,
    )
    db.add(piece)
    db.commit()
    piece.created_at = utcnow() - timedelta(days=400)
    db.commit()

    _usage(db, age_days=40)
    llm_usage.purge(db, days=30)

    assert db.query(Content).count() == 1
