"""One publish request, end to end, while the broker dies underneath it.

Everything in this flow was already tested in pieces: ``_dispatch``'s resume
accounting against a fake task, ``publish_one``'s conditional claim against a
hand-built row, the adapters against stubbed HTTP. What nothing covered is the
three of them together on a real request — which is the only place the property
that matters is observable.

That property is: **the user clicks publish once, and each platform is posted
exactly once, whatever the broker does mid-batch.** It has two halves that fail
in opposite directions and are guarded in different modules, so testing either
alone proves nothing about the pair:

* drop nothing — ``_dispatch`` falls through to running inline, so a broker that
  refuses the handoff does not silently lose the publish (routers/content.py);
* post nothing twice — the inline loop resumes from where the broker stopped,
  and ``publish_one``'s conditional UPDATE to ``PUBLISHING`` catches anything
  that slips past that (tasks/publish_tasks.py).

The second half is why these run with the real ``publish_one`` rather than a
recorder. A test that counts dispatches cannot tell a re-dispatch that was
harmlessly skipped from one that posted to Dev.to twice; only the adapter can,
so the adapter is what gets counted here.

``settings.celery_enabled`` is forced on: with it off the request never reaches
the branch under test, which is exactly why this gap survived so long.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, PublicationStatus
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import PublishResult
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.medium import MediumAdapter
from app.tasks import publish_tasks

API = "/api/v1/content"

#: The three the batch cross-posts to, in the order the request lists them.
PLATFORMS = [Platform.DEVTO, Platform.HASHNODE, Platform.MEDIUM]
ADAPTERS = {
    Platform.DEVTO: DevToAdapter,
    Platform.HASHNODE: HashnodeAdapter,
    Platform.MEDIUM: MediumAdapter,
}


def _no_close(session):
    """Hand the task the test's session without letting it close the shared one."""

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
        status=ContentStatus.APPROVED,
        title="Herald survives a dead broker",
        slug="herald-survives-a-dead-broker",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user) -> None:
    db.add_all(
        [
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"api_key": "k"}),
                display_name="@herald",
            )
            for platform in PLATFORMS
        ]
    )
    db.commit()


@pytest.fixture
def posts(db, monkeypatch) -> list[Platform]:
    """Record every call that actually reaches a platform.

    One list across all three adapters, so a double-post shows up as a repeated
    entry rather than as a count that has to be compared against a total.
    """
    recorded: list[Platform] = []

    def _install(platform, adapter):
        def _publish(self, request, credentials):
            recorded.append(platform)
            slug = platform.value
            return PublishResult(
                external_id=f"{slug}-1",
                external_url=f"https://{slug}.example/herald",
            )

        monkeypatch.setattr(adapter, "publish", _publish)

    for platform, adapter in ADAPTERS.items():
        _install(platform, adapter)

    # The publish runs on the request thread via the inline fallback, so the
    # task must use the test's session or it writes to an empty database.
    monkeypatch.setattr(publish_tasks, "SessionLocal", _no_close(db))
    monkeypatch.setattr(settings, "celery_enabled", True)
    return recorded


def _publish(client, auth, content, project, db):
    """Issue the request the user's publish button issues."""
    # No canonical platform: the syndication stagger deliberately parks the
    # copies behind the original, and a batch of one dispatched id would not
    # exercise the resume accounting this file is about.
    project.canonical_platform = None
    project.auto_canonical = False
    db.commit()
    return client.post(
        f"{API}/{content.id}/publish",
        headers=auth,
        json={"platforms": [p.value for p in PLATFORMS]},
    )


def _dies_after(n: int, monkeypatch):
    """Make ``publish_one.delay`` accept *n* ids and then refuse."""
    accepted: list[int] = []

    def _delay(publication_id):
        if len(accepted) >= n:
            raise ConnectionError("redis is not listening")
        accepted.append(publication_id)

    monkeypatch.setattr(publish_tasks.publish_one, "delay", _delay)
    return accepted


def test_a_broker_that_dies_mid_batch_posts_each_platform_exactly_once(
    client, auth, db, content, project, connected, posts, monkeypatch
):
    """The whole point, in one assertion.

    The broker takes the first id and then dies. Before the resume fix the
    inline loop restarted at the top, so the first publication's full attempt —
    render, resolve credentials, call the adapter — ran a second time on the
    request thread, and only ``publish_one``'s claim stopped it reaching Dev.to.
    """
    accepted = _dies_after(1, monkeypatch)

    resp = _publish(client, auth, content, project, db)

    assert resp.status_code == 200, resp.text
    assert len(accepted) == 1, "the broker took exactly one before dying"
    # Not `== 2`: the assertion is about *which* platforms, because a duplicate
    # and a dropped one would cancel out in a count.
    assert posts == [Platform.HASHNODE, Platform.MEDIUM], (
        "the id the broker accepted belongs to a worker; the rest run here"
    )


def test_the_request_still_succeeds_and_reports_every_platform(
    client, auth, db, content, project, connected, posts, monkeypatch
):
    """The user asked for three platforms and must be told about three.

    A broker outage is an infrastructure problem, not a bad request, and the
    response is what the editor renders its publication list from.
    """
    _dies_after(1, monkeypatch)

    resp = _publish(client, auth, content, project, db)

    assert resp.status_code == 200, resp.text
    assert {row["platform"] for row in resp.json()} == {p.value for p in PLATFORMS}


def test_a_broker_that_is_dead_from_the_first_id_still_publishes_all_three(
    client, auth, db, content, project, connected, posts, monkeypatch
):
    """Redis already down when the request arrives — the common outage shape.

    Nothing was handed off, so the inline loop owns the whole batch.
    """
    accepted = _dies_after(0, monkeypatch)

    resp = _publish(client, auth, content, project, db)

    assert resp.status_code == 200, resp.text
    assert accepted == []
    assert posts == PLATFORMS


def test_a_healthy_broker_publishes_nothing_on_the_request_thread(
    client, auth, db, content, project, connected, posts, monkeypatch
):
    """The control. Without it the three tests above would pass on a
    ``_dispatch`` that ignored the broker entirely and always ran inline."""
    accepted = _dies_after(99, monkeypatch)

    resp = _publish(client, auth, content, project, db)

    assert resp.status_code == 200, resp.text
    assert len(accepted) == 3, "all three went to the broker"
    assert posts == [], "and none of them touched a platform here"


def test_every_publication_row_lands_published_after_the_outage(
    client, auth, db, content, project, connected, posts, monkeypatch
):
    """The rows are what the beat sweep and the UI read next.

    A publication left in ``PUBLISHING`` reads as claimed-by-a-worker, and the
    worker that would have finished it is the one the broker never got. It sits
    there until ``reclaim_stuck`` times it out — so "the post went out" and "the
    row says so" are separate facts and both have to hold.
    """
    _dies_after(1, monkeypatch)

    resp = _publish(client, auth, content, project, db)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    rows = {p.platform: p for p in content.publications}
    assert set(rows) == set(PLATFORMS)

    # The one the broker accepted is still PENDING — a worker owns it, and this
    # request correctly did not touch it.
    assert rows[Platform.DEVTO].status == PublicationStatus.PENDING
    for platform in (Platform.HASHNODE, Platform.MEDIUM):
        row = rows[platform]
        assert row.status == PublicationStatus.PUBLISHED, platform
        assert row.external_url == f"https://{platform.value}.example/herald"
        assert row.attempts == 1, "one attempt each, not a retried duplicate"
