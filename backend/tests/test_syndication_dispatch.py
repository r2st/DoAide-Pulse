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
from app.models.mixins import as_aware
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


# --------------------------------------------------------------------------- #
# The hand retry, which went round both halves of the stagger                  #
# --------------------------------------------------------------------------- #
#
# ``routers.content.retry_publication`` cleared ``scheduled_for`` and dispatched
# the row on the spot. That is right for the case it was written for — a human
# clicking retry knows things the backoff does not — and wrong for a syndicated
# copy whose original has not published yet: the copy skipped the queue-time
# stagger *and* the claim guard, because a row with no ``scheduled_for`` is due
# now however it came to be that way. It then went out carrying no canonical
# URL, and unlike the queue-time race that one does not come back: the copy is
# live on the platform with nothing pointing at the original.


def _crosspost(db, content, project, *, canonical=Platform.DEVTO):
    """A queued cross-post: the original, and one copy parked behind it."""
    project.canonical_platform = canonical
    db.commit()
    publishing_service.queue(db, content, ["devto", "medium"])
    db.commit()
    by_platform = {p.platform: p for p in content.publications}
    return by_platform[Platform.DEVTO], by_platform[Platform.MEDIUM]


def _fail(db, publication):
    """Put a row in the state the retry button is offered for."""
    publication.status = PublicationStatus.FAILED
    publication.scheduled_for = None
    publication.attempts = 3
    publication.error = "medium said no"
    db.commit()


def _retry(client, auth, content, publication):
    return client.post(
        f"/api/v1/content/{content.id}/retry/{publication.id}", headers=auth
    )


def test_retrying_a_copy_does_not_jump_the_canonical(
    db, client, auth, content, project, connected, monkeypatch
):
    """The bug: the copy went out immediately, before the original had a URL."""
    devto, medium = _crosspost(db, content, project)
    _fail(db, medium)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, medium)
    assert resp.status_code == 200, resp.text

    db.refresh(medium)
    assert medium.status == PublicationStatus.SCHEDULED
    assert medium.scheduled_for is not None
    assert as_aware(medium.scheduled_for) > datetime.now(UTC)
    # Not handed to a worker either: ``publish_one`` would refuse the claim, and
    # the row's route to a platform is now the beat sweep that checks its time.
    assert dispatched == []
    # The response says so too, rather than reporting a retry that is not
    # happening yet — this is what the publications list renders.
    assert resp.json()["status"] == PublicationStatus.SCHEDULED.value
    assert resp.json()["scheduled_for"] is not None
    # Re-armed all the same: the attempt count and the stale error are gone.
    assert medium.attempts == 0
    assert medium.error is None
    assert devto.status == PublicationStatus.PENDING


def test_retrying_the_original_itself_still_goes_out_now(
    db, client, auth, content, project, connected, monkeypatch
):
    """The original's whole job is to go first. Nothing to hold it behind."""
    devto, _medium = _crosspost(db, content, project)
    _fail(db, devto)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, devto)
    assert resp.status_code == 200, resp.text

    db.refresh(devto)
    assert devto.status == PublicationStatus.PENDING
    assert devto.scheduled_for is None
    assert dispatched == [devto.id]


def test_retrying_a_copy_goes_out_now_once_the_original_has_its_url(
    db, client, auth, content, project, connected, monkeypatch
):
    """With a canonical URL to carry, the copy has nothing left to wait for."""
    _devto, medium = _crosspost(db, content, project)
    _fail(db, medium)
    content.canonical_url = "https://dev.to/r2st/herald-1-0"
    db.commit()

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, medium)
    assert resp.status_code == 200, resp.text

    db.refresh(medium)
    assert medium.status == PublicationStatus.PENDING
    assert medium.scheduled_for is None
    assert dispatched == [medium.id]


def test_retrying_a_copy_whose_original_gave_up_does_not_wait_for_it(
    db, client, auth, content, project, connected, monkeypatch
):
    """A terminal original is not going to produce the URL, so waiting is pure delay.

    This is the case that makes the hold a delay rather than a dependency: the
    copy is the only publication left that can still succeed, and a user
    retrying it is asking for the piece to be *somewhere*.
    """
    devto, medium = _crosspost(db, content, project)
    _fail(db, devto)
    _fail(db, medium)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, medium)
    assert resp.status_code == 200, resp.text

    db.refresh(medium)
    assert medium.status == PublicationStatus.PENDING
    assert medium.scheduled_for is None
    assert dispatched == [medium.id]


def test_a_held_retry_waits_for_the_original_not_for_the_click(
    db, content, project, connected
):
    """The distance is measured from when the original is due, as ``queue`` does.

    A copy retried while the original is still parked two hours out and held
    only ``syndication_delay_seconds`` from *now* would publish an hour and
    three quarters before it.
    """
    project.canonical_platform = Platform.DEVTO
    db.commit()
    later = datetime.now(UTC) + timedelta(hours=2)
    publishing_service.queue(db, content, ["devto", "medium"], scheduled_for=later)
    db.commit()
    medium = next(p for p in content.publications if p.platform == Platform.MEDIUM)
    _fail(db, medium)

    hold = publishing_service.retry_hold(content, medium)

    assert hold is not None
    expected = later + timedelta(seconds=settings.syndication_delay_seconds)
    assert abs((hold - expected).total_seconds()) < 5


def test_a_held_retry_is_not_pushed_into_the_past_by_an_overdue_original(
    db, content, project, connected
):
    """An original whose time came and went is due *now*, not two hours ago."""
    project.canonical_platform = Platform.DEVTO
    db.commit()
    publishing_service.queue(db, content, ["devto", "medium"])
    db.commit()
    devto, medium = (
        {p.platform: p for p in content.publications}[k]
        for k in (Platform.DEVTO, Platform.MEDIUM)
    )
    devto.status = PublicationStatus.SCHEDULED
    devto.scheduled_for = datetime.now(UTC) - timedelta(hours=2)
    db.commit()
    _fail(db, medium)

    hold = publishing_service.retry_hold(content, medium)

    assert hold is not None
    assert hold > datetime.now(UTC)


def test_a_single_platform_retry_is_immediate_as_before(
    db, client, auth, content, project, connected, monkeypatch
):
    """The ordinary retry — one platform, nothing to syndicate — is untouched.

    ``test_publish_retry_backoff`` states why this matters: a parked row must
    not make "retry now" mean "retry in twenty minutes".
    """
    project.canonical_platform = Platform.DEVTO
    db.commit()
    devto = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    _fail(db, devto)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, devto)
    assert resp.status_code == 200, resp.text

    db.refresh(devto)
    assert devto.status == PublicationStatus.PENDING
    assert devto.scheduled_for is None
    assert dispatched == [devto.id]


def test_a_retry_is_immediate_when_the_stagger_is_switched_off(
    db, client, auth, content, project, connected, monkeypatch
):
    _devto, medium = _crosspost(db, content, project)
    _fail(db, medium)
    monkeypatch.setattr(
        "app.services.publishing_service.settings.syndication_delay_seconds", 0
    )

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, medium)
    assert resp.status_code == 200, resp.text

    db.refresh(medium)
    assert medium.scheduled_for is None
    assert dispatched == [medium.id]


def test_a_retry_of_a_social_copy_is_immediate(
    db, client, auth, content, project, connected, monkeypatch
):
    """Nothing in this batch can hold a canonical URL, so nothing is an original.

    ``_original_platform`` answers ``None`` and the copies have nothing to wait
    for — the same reasoning that keeps ``queue`` from staggering a batch of
    social posts.
    """
    project.canonical_platform = None
    project.auto_canonical = True
    db.commit()
    publishing_service.queue(db, content, ["bluesky", "mastodon"])
    db.commit()
    bluesky = next(p for p in content.publications if p.platform == Platform.BLUESKY)
    _fail(db, bluesky)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.routers.content._dispatch", lambda ids: dispatched.extend(ids)
    )

    resp = _retry(client, auth, content, bluesky)
    assert resp.status_code == 200, resp.text

    db.refresh(bluesky)
    assert bluesky.status == PublicationStatus.PENDING
    assert bluesky.scheduled_for is None
    assert dispatched == [bluesky.id]


def test_the_held_retry_is_still_picked_up_by_the_beat_sweep(
    db, client, auth, content, project, connected
):
    """Held, not dropped. The beat sweep is the row's only route out now."""
    _devto, medium = _crosspost(db, content, project)
    _fail(db, medium)

    assert _retry(client, auth, content, medium).status_code == 200
    db.refresh(medium)

    later = as_aware(medium.scheduled_for) + timedelta(seconds=1)
    due = publishing_service.due_publications(db, now=later)

    assert medium.id in [p.id for p in due]


def test_a_held_retry_is_not_claimed_before_its_time(
    db, client, auth, content, project, connected, monkeypatch
):
    """The claim guard is the backstop, and it covers the retried row too."""
    _devto, medium = _crosspost(db, content, project)
    _fail(db, medium)

    assert _retry(client, auth, content, medium).status_code == 200

    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    result = publish_tasks.publish_one(medium.id)

    assert result["skipped"] is True
    db.refresh(medium)
    assert medium.status == PublicationStatus.SCHEDULED
