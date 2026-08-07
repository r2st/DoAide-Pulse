"""The syndication stagger has to survive the dispatch, not just the queue.

``publishing_service.queue`` parks the syndicated copies of a cross-post behind
the canonical platform by ``syndication_delay_seconds`` — that much was already
covered in ``test_canonical.py``. What was not covered is what happens to those
rows next, and it turned out to be "they go out immediately anyway":

* both callers that publish a whole batch — ``routers.content._queue_publish``
  and ``services.content_pipeline.generate_and_route`` — handed *every*
  publication in the batch to ``publish_one``, including the parked ones;
* ``publish_one`` claimed any row in ``PENDING`` or ``SCHEDULED`` without
  looking at ``scheduled_for``.

So the copies published in the same instant as the original, which is the one
outcome the stagger exists to prevent: a copy dispatched before the original has
an ``external_url`` cannot carry a canonical link back to it, and a crawler has
no reason to believe the original came first.

The claim guard is the load-bearing half — it is what makes the row safe
whoever dispatches it, including a stale task replayed by a broker — so it is
tested directly as well as through the two callers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.tasks import publish_tasks


def _no_close(session):
    """Hand a task the test's session without letting it close the shared one."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        status=ContentStatus.APPROVED,
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    """Live credentials for the two platforms these tests cross-post to."""
    db.add_all(
        [
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"api_key": "k"}),
                display_name="@r2st",
            )
            for platform in (Platform.DEVTO, Platform.MEDIUM)
        ]
    )
    db.commit()


# --------------------------------------------------------------------------- #
# The claim itself                                                             #
# --------------------------------------------------------------------------- #


def test_publish_one_will_not_claim_a_row_parked_in_the_future(
    db, content, monkeypatch
):
    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    publication = publishing_service.queue(
        db, content, ["devto"], scheduled_for=datetime.now(UTC) + timedelta(hours=2)
    )[0]

    result = publish_tasks.publish_one(publication.id)

    assert result["skipped"] is True
    db.refresh(publication)
    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.attempts == 0


def test_publish_one_claims_a_scheduled_row_whose_time_has_come(
    db, content, monkeypatch
):
    """The guard is about the clock, not about the status."""
    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    publication = publishing_service.queue(db, content, ["devto"])[0]
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    result = publish_tasks.publish_one(publication.id)

    assert not result.get("skipped")


def test_publish_one_still_claims_an_unscheduled_pending_row(db, content, monkeypatch):
    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    publication = publishing_service.queue(db, content, ["devto"])[0]
    assert publication.scheduled_for is None

    result = publish_tasks.publish_one(publication.id)

    assert not result.get("skipped")


def test_a_rate_limited_row_is_left_alone_until_its_deferral_elapses(
    db, content, monkeypatch
):
    """``_defer`` parks a row the same way the stagger does, for the same reason."""
    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    publication = publishing_service.queue(db, content, ["devto"])[0]
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = datetime.now(UTC) + timedelta(minutes=30)
    publication.attempts = 1
    db.commit()

    result = publish_tasks.publish_one(publication.id)

    assert result["skipped"] is True
    db.refresh(publication)
    # Coming back sooner than the platform asked is how a soft limit becomes a
    # ban, so the attempt must not have been spent either.
    assert publication.attempts == 1


# --------------------------------------------------------------------------- #
# The two callers that dispatch a whole batch                                  #
# --------------------------------------------------------------------------- #


def test_an_immediate_crosspost_dispatches_only_the_canonical(
    db, client, auth, content, project, connected, monkeypatch
):
    project.canonical_platform = Platform.DEVTO
    db.commit()

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        json={"platforms": ["devto", "medium"]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    by_platform = {p.platform: p for p in content.publications}
    devto = by_platform[Platform.DEVTO]
    medium = by_platform[Platform.MEDIUM]

    assert medium.status == PublicationStatus.SCHEDULED
    assert medium.scheduled_for is not None
    assert dispatched == [devto.id]


def test_a_single_platform_publish_is_dispatched_as_before(
    db, client, auth, content, project, connected, monkeypatch
):
    """Nothing to stagger means nothing held back — the ordinary case."""
    project.canonical_platform = Platform.DEVTO
    db.commit()

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        json={"platforms": ["devto"]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(dispatched) == 1


def test_the_autopilot_holds_its_syndicated_copies_back_too(
    db, project, connected, monkeypatch
):
    """``content_pipeline`` dispatches its own batch and had the same hole."""
    from app.models.project import AutopilotMode
    from app.services import content_pipeline

    project.canonical_platform = Platform.DEVTO
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto", "medium"]
    db.commit()

    published: list[int] = []
    monkeypatch.setattr(content_pipeline, "publish_now", published.append)
    # The template fallback is what runs with no provider keys, so both quality
    # gates have to be opened for the auto-publish branch to be reached at all.
    monkeypatch.setattr(settings, "autopilot_auto_publish_confidence", 0.0)
    monkeypatch.setattr(settings, "link_check_enabled", False)
    monkeypatch.setattr(content_pipeline.seo, "SEO_SCORE_THRESHOLD", 0)

    routed = content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )
    assert routed.auto_published, routed.status

    parked = [
        p
        for p in db.query(Publication).filter_by(content_id=routed.content.id)
        if p.scheduled_for is not None
    ]
    assert [p.platform for p in parked] == [Platform.MEDIUM]
    assert parked[0].id not in published


def test_the_stagger_is_off_when_the_delay_is_zero(
    db, client, auth, content, project, connected, monkeypatch
):
    """A project that wants everything at once still gets everything at once."""
    monkeypatch.setattr(
        "app.services.publishing_service.settings.syndication_delay_seconds", 0
    )
    project.canonical_platform = Platform.DEVTO
    db.commit()

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = client.post(
        f"/api/v1/content/{content.id}/publish",
        json={"platforms": ["devto", "medium"]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(dispatched) == 2


def test_the_beat_sweep_picks_the_copy_up_once_the_delay_is_up(db, content, project):
    """The stagger delays the copy; it must not lose it."""
    project.canonical_platform = Platform.DEVTO
    db.commit()

    publications = publishing_service.queue(db, content, ["devto", "medium"])
    medium = next(p for p in publications if p.platform == Platform.MEDIUM)

    later = medium.scheduled_for + timedelta(seconds=1)
    due = publishing_service.due_publications(db, now=later)

    assert medium.id in [p.id for p in due]
