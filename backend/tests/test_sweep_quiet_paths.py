"""What the beat sweeps do on the days nothing needs doing.

Every sweep in Pulse runs on a schedule whether or not there is work, so the
"nothing to do" arm runs far more often than the working one. It is also the arm
that is easiest to get wrong in a way nobody notices: a sweep that logs a swap it
did not make, or reports a digest it did not send, is misinformation on a daily
cron.

The assertions are therefore about the *counters and the side effects*, not just
the return value — a sweep that returns ``{"swapped": 0}`` while having changed a
headline is the failure worth catching.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.password_reset import PasswordResetToken
from app.models.project import AutopilotMode
from app.models.publication import Platform, Publication, PublicationStatus
from app.tasks import autopilot_tasks
from app.tasks.digest_tasks import send_weekly_digests
from app.tasks.headline_tasks import auto_select_headlines
from app.tasks.maintenance_tasks import purge_expired_tokens

from .conftest import repo_activity as make_activity


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# The autopilot scan                                                          #
# --------------------------------------------------------------------------- #


def test_a_scan_that_finds_nothing_new_says_so_and_writes_nothing(
    db, project, stub_github
):
    """The overwhelmingly common outcome: the repo has not moved since last hour.

    Distinct from ``below_threshold``, which is "something happened but not
    enough" and holds the watermark. Nothing happened at all, so there is
    nothing to hold and nothing to bank.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    project.last_seen_commit_sha = "old"
    db.commit()
    stub_github["set"](make_activity(commits=0, release=False, head="old"))

    result = autopilot_tasks.scan_project(project.id)

    assert result["status"] == "no_news"
    assert db.query(Content).count() == 0
    db.refresh(project)
    # It did look, so the scan timestamp moves even though nothing else does.
    assert project.last_scanned_at is not None


# --------------------------------------------------------------------------- #
# The headline sweep                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def contested(db, project):
    """A published piece with a headline history that will *not* justify a swap.

    Both headlines earn the same rate, so the winner is called noise and nothing
    is applied — a real candidate that the sweep must count and leave alone.
    """
    project.auto_headline_winner = True
    db.commit()

    start = _now() - timedelta(days=20)
    piece = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Challenger headline",
        slug="a-contest",
        status=ContentStatus.PUBLISHED,
        created_at=start,
        headline_history=[
            {"title": "Original headline", "changed_at": (start + timedelta(days=10)).isoformat()}
        ],
    )
    db.add(piece)
    db.commit()
    db.refresh(piece)

    publication = Publication(
        content_id=piece.id, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    # 1000 views either side of the swap: a dead heat, which is noise.
    for offset, views in ((1, 500), (9, 1000), (11, 1500), (19, 2000)):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=start + timedelta(days=offset),
                views=views,
            )
        )
    db.commit()
    return piece


def test_a_candidate_the_evidence_does_not_back_is_counted_but_not_swapped(
    db, contested, task_session, monkeypatch
):
    """``considered`` moves, ``swapped`` does not, and the title is untouched."""
    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", task_session)

    result = auto_select_headlines()

    assert result == {"considered": 1, "swapped": 0}
    db.refresh(contested)
    assert contested.title == "Challenger headline"


def test_the_sweep_over_nothing_at_all_is_a_pair_of_zeros(db, task_session, monkeypatch):
    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", task_session)

    assert auto_select_headlines() == {"considered": 0, "swapped": 0}


# --------------------------------------------------------------------------- #
# The digest sweep                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def smtp_configured(monkeypatch):
    """The sweep bails out before touching the database without this."""
    monkeypatch.setattr("app.services.mailer.configured", lambda: True)


def test_the_sweep_does_not_open_a_session_when_smtp_is_not_configured(monkeypatch):
    """A page of digest in the logs every Monday, for nobody, is worse than none."""
    monkeypatch.setattr("app.services.mailer.configured", lambda: False)
    monkeypatch.setattr(
        "app.tasks.digest_tasks.SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("no session should be opened")),
    )

    assert send_weekly_digests() == {
        "considered": 0,
        "sent": 0,
        "skipped": "smtp-not-configured",
    }


def test_a_user_whose_week_was_empty_is_considered_but_not_mailed(
    db, user, task_session, smtp_configured, monkeypatch
):
    """An empty digest is not an error and not a send — see ``digest``'s docstring.

    The counter has to tell those apart, because "considered 1, sent 0" is a
    quiet week and "considered 0" is a sweep that could not see the user at all.
    """
    user.weekly_digest_enabled = True
    db.commit()
    monkeypatch.setattr("app.tasks.digest_tasks.SessionLocal", task_session)

    sent: list = []
    monkeypatch.setattr(
        "app.services.mailer.send", lambda **kwargs: sent.append(kwargs) or True
    )

    result = send_weekly_digests()

    assert result == {"considered": 1, "sent": 0}
    assert sent == []


def test_a_user_who_switched_the_digest_off_is_not_even_considered(
    db, user, task_session, smtp_configured, monkeypatch
):
    user.weekly_digest_enabled = False
    db.commit()
    monkeypatch.setattr("app.tasks.digest_tasks.SessionLocal", task_session)

    assert send_weekly_digests() == {"considered": 0, "sent": 0}


# --------------------------------------------------------------------------- #
# Housekeeping                                                                #
# --------------------------------------------------------------------------- #


def test_purging_nothing_reports_nothing_and_logs_nothing(db, task_session, monkeypatch, caplog):
    """The common case, every day, forever. It must be silent."""
    monkeypatch.setattr("app.tasks.maintenance_tasks.SessionLocal", task_session)

    with caplog.at_level("INFO", logger="app.tasks.maintenance_tasks"):
        assert purge_expired_tokens() == {"purged": 0}

    assert caplog.records == []


def test_a_spent_token_is_purged_and_said_out_loud(db, user, task_session, monkeypatch, caplog):
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash="a" * 64,
            expires_at=_now() - timedelta(days=2),
        )
    )
    db.commit()
    monkeypatch.setattr("app.tasks.maintenance_tasks.SessionLocal", task_session)

    with caplog.at_level("INFO", logger="app.tasks.maintenance_tasks"):
        assert purge_expired_tokens() == {"purged": 1}

    assert any("purged 1" in record.getMessage() for record in caplog.records)
