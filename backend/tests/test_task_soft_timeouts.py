"""The remaining beat loops, at the moment the soft time limit lands.

Celery raises ``SoftTimeLimitExceeded`` *inside* whatever the task is doing.
There are seconds left before the hard limit kills the worker outright, and the
only useful thing a sweep can do with them is stop, keep what it has already
committed, and report honestly — the next scheduled run picks up the rest.

Three loops had that branch and nothing walking it. Each has its own hazard:

* **digests** must not re-mail the users it already sent to, so a break has to
  return a truthful ``sent`` count;
* **headlines** holds an uncommitted swap when the limit lands, and has to roll
  it back rather than leave a half-applied title;
* **autopilot's per-project scan** must not move the commit watermark, or the
  commits it never wrote about are silently skipped forever.

``publish_tasks``, ``metrics_tasks``, ``webhook_tasks`` and ``trigger_tasks``
already have theirs — see ``test_publish_task_paths.py``,
``test_metrics_task_paths.py``, ``test_webhook_sweep.py`` and
``test_beat_tasks.py``.
"""
from __future__ import annotations

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone
from app.models.user import User
from app.security import hash_password
from app.services import digest, headlines, mailer
from app.tasks import autopilot_tasks, digest_tasks, headline_tasks


def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _task_session(db, monkeypatch):
    for module in (digest_tasks, headline_tasks, autopilot_tasks):
        monkeypatch.setattr(module, "SessionLocal", _no_close(db))


# --------------------------------------------------------------------------- #
# send_weekly_digests                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def subscribers(db, user) -> list[User]:
    """Three subscribed users, so "stopped part-way" is distinguishable."""
    rows = [user]
    for i in range(2):
        row = User(
            email=f"reader{i}@example.com",
            full_name=f"Reader {i}",
            hashed_password=hash_password("hunter2hunter2"),
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(mailer, "configured", lambda: True)


def test_a_digest_sweep_that_runs_out_of_time_keeps_what_it_sent(
    db, subscribers, smtp, monkeypatch
):
    sent: list[int] = []

    def _send(session, who):
        if len(sent) == 1:
            raise SoftTimeLimitExceeded()
        sent.append(who.id)
        return True

    monkeypatch.setattr(digest, "send", _send)

    result = digest_tasks.send_weekly_digests()

    # One mailed, one interrupted, one never reached — and the count says so
    # rather than claiming the whole list went out.
    assert result == {"considered": 2, "sent": 1}
    assert len(sent) == 1


def test_a_digest_sweep_does_not_retry_the_user_it_timed_out_on(
    db, subscribers, smtp, monkeypatch
):
    """The break is a break, not a continue. Re-entering the loop with seconds
    left before the hard limit is how a worker gets killed mid-send.
    """
    attempts: list[int] = []

    def _send(session, who):
        attempts.append(who.id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(digest, "send", _send)

    result = digest_tasks.send_weekly_digests()

    assert result == {"considered": 1, "sent": 0}
    assert len(attempts) == 1


def test_one_users_digest_failing_does_not_stop_the_others(
    db, subscribers, smtp, monkeypatch
):
    """The neighbouring branch, and the distinction it exists for: an ordinary
    exception is one bad week, a timeout is the whole sweep being out of road.
    """
    seen: list[int] = []

    def _send(session, who):
        seen.append(who.id)
        if len(seen) == 1:
            raise RuntimeError("that user's metrics are a mess")
        return True

    monkeypatch.setattr(digest, "send", _send)

    result = digest_tasks.send_weekly_digests()

    assert result == {"considered": 3, "sent": 2}


def test_a_digest_sweep_without_smtp_never_opens_a_session(monkeypatch, subscribers):
    monkeypatch.setattr(mailer, "configured", lambda: False)

    result = digest_tasks.send_weekly_digests()

    assert result["skipped"] == "smtp-not-configured"
    assert result == {"considered": 0, "sent": 0, "skipped": "smtp-not-configured"}


# --------------------------------------------------------------------------- #
# auto_select_headlines                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def contested(db, user) -> list[Content]:
    """Three published pieces on opted-in projects, each with a contest."""
    rows = []
    for i in range(3):
        project = Project(
            user_id=user.id,
            name=f"Headline {i}",
            slug=f"headline-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
            auto_headline_winner=True,
        )
        db.add(project)
        db.flush()
        content = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.PUBLISHED,
            title=f"Original {i}",
            slug=f"original-{i}",
            body_markdown="Body.",
            headline_history=[{"title": f"Variant {i}", "at": "2026-01-01T00:00:00Z"}],
        )
        db.add(content)
        rows.append(content)
    db.commit()
    return rows


def test_a_headline_sweep_that_runs_out_of_time_stops_where_it_is(
    db, contested, monkeypatch
):
    seen: list[int] = []

    def _auto_select(content, session):
        seen.append(content.id)
        if len(seen) == 2:
            raise SoftTimeLimitExceeded()
        return _verdict(applied=True), True

    monkeypatch.setattr(headlines, "auto_select", _auto_select)

    result = headline_tasks.auto_select_headlines()

    assert result == {"considered": 2, "swapped": 1}
    assert len(seen) == 2


def test_a_headline_sweep_rolls_back_the_swap_it_was_holding(
    db, contested, monkeypatch
):
    """The limit lands between ``auto_select`` mutating the row and the commit.

    Without the rollback that half-applied title is still in the session, and
    the next thing to commit on it — anything at all — writes a headline the
    sweep never decided on.
    """
    titles = [c.title for c in contested]

    def _auto_select(content, session):
        content.title = "Half-applied"
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(headlines, "auto_select", _auto_select)

    result = headline_tasks.auto_select_headlines()

    assert result == {"considered": 1, "swapped": 0}
    db.expire_all()
    assert [c.title for c in contested] == titles


def test_one_headline_failing_does_not_stop_the_sweep(db, contested, monkeypatch):
    seen: list[int] = []

    def _auto_select(content, session):
        seen.append(content.id)
        if len(seen) == 1:
            raise RuntimeError("no metrics for that one")
        return _verdict(applied=True), True

    monkeypatch.setattr(headlines, "auto_select", _auto_select)

    result = headline_tasks.auto_select_headlines()

    assert result == {"considered": 3, "swapped": 2}


def test_a_headline_sweep_with_no_candidates_reports_zero(db):
    assert headline_tasks.auto_select_headlines() == {"considered": 0, "swapped": 0}


class _verdict:
    """The shape ``auto_select`` returns — only ``reason`` is read here."""

    def __init__(self, applied: bool):
        self.applied = applied
        self.reason = "test"


# --------------------------------------------------------------------------- #
# scan_project                                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo_project(db, user) -> Project:
    row = Project(
        user_id=user.id,
        name="Pulse",
        slug="pulse-scan",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
        repo_url="https://github.com/r2st/DoAide-Pulse",
        last_seen_commit_sha="abc123",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_scan_that_runs_out_of_time_says_so(db, repo_project, monkeypatch):
    monkeypatch.setattr(
        autopilot_tasks.github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    result = autopilot_tasks.scan_project(repo_project.id)

    assert result == {"project_id": repo_project.id, "status": "timeout"}


def test_a_scan_that_runs_out_of_time_does_not_move_the_watermark(
    db, repo_project, monkeypatch
):
    """The watermark is what makes the next scan skip these commits. Moving it
    on a scan that never finished is how news gets silently dropped — the run
    that would have written about it never sees it again.
    """
    monkeypatch.setattr(
        autopilot_tasks.github_client,
        "fetch_activity",
        lambda *a, **k: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    autopilot_tasks.scan_project(repo_project.id)

    db.expire_all()
    assert repo_project.last_seen_commit_sha == "abc123"
    assert repo_project.last_scanned_at is None
