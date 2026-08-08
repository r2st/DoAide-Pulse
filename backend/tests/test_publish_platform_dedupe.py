"""A platform named twice in one request must queue it once.

``["devto", "devto"]`` is not a request to publish twice — there is nowhere for
the second copy to go, and ``uq_publication_content_platform`` says so at the
database level. Before :func:`app.schemas.content.unique_platforms` the repeat
reached :func:`app.services.publishing_service.queue`, whose "already queued"
index is built once *before* its loop and so never saw the second mention as a
repeat: the INSERT hit the constraint and the caller got a 500 for a payload the
API had already validated.

Every entry point that takes a platform list is covered here, because they do
not share one code path — publish and bulk publish reach ``queue`` directly,
while schedule sizes its slot list from the same list first.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication


@pytest.fixture
def connected(db, user):
    """Dev.to and Mastodon connected, so the publish guards pass."""
    for platform in (Platform.DEVTO, Platform.MASTODON):
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                encrypted_credentials="not-read-in-this-test",
                status=ConnectionStatus.CONNECTED,
            )
        )
    db.commit()


@pytest.fixture
def approved(db, project):
    content = Content(
        project_id=project.id,
        title="Ship it",
        slug="ship-it",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        body_markdown="A body with no links in it at all.",
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


def _publications(db, content_id: int) -> list[Publication]:
    return list(db.query(Publication).filter(Publication.content_id == content_id))


def _soon() -> str:
    """A time far enough ahead to schedule against.

    Every publish here is scheduled rather than immediate, following the same
    convention as ``tests/test_content.py``: an immediate publish dispatches
    inline under ``CELERY_ENABLED=false`` and would try to reach Dev.to. The
    constraint violation this module is about happens in
    ``publishing_service.queue``, well before any dispatch, so scheduling costs
    the tests nothing.
    """
    return (utcnow() + timedelta(days=2)).isoformat()


def test_publishing_to_the_same_platform_twice_queues_one_row(
    client, auth, db, approved, connected
):
    resp = client.post(
        f"/api/v1/content/{approved.id}/publish",
        json={"platforms": ["devto", "devto"], "scheduled_for": _soon()},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.json()[0]["platform"] == "devto"
    assert len(_publications(db, approved.id)) == 1


def test_the_repeat_does_not_disturb_the_other_platforms_in_the_batch(
    client, auth, db, approved, connected
):
    """A repeat is dropped; everything else in the list still goes out."""
    resp = client.post(
        f"/api/v1/content/{approved.id}/publish",
        json={
            "platforms": ["devto", "mastodon", "devto"],
            "scheduled_for": _soon(),
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert sorted(p["platform"] for p in resp.json()) == ["devto", "mastodon"]
    assert len(_publications(db, approved.id)) == 2


def test_scheduling_to_the_same_platform_twice_books_one_slot(
    client, auth, db, approved, connected
):
    """The schedule path counts platforms before any row is written.

    ``optimize`` sizes its slot list from this list, so a repeat there books two
    slots for one destination even in the runs where the INSERT would have
    survived.
    """
    resp = client.post(
        f"/api/v1/content/{approved.id}/schedule",
        json={"platforms": ["devto", "devto"], "scheduled_for": _soon()},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert len(_publications(db, approved.id)) == 1


def test_bulk_publishing_to_the_same_platform_twice_queues_one_row(
    client, auth, db, approved, connected
):
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={
            "content_ids": [approved.id],
            "platforms": ["devto", "devto"],
            "scheduled_for": _soon(),
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["succeeded"] == [approved.id]
    assert resp.json()["failed"] == []
    assert len(_publications(db, approved.id)) == 1


def test_a_repeated_platform_query_param_suggests_one_slot(
    client, auth, approved, connected
):
    """``?platforms=devto&platforms=devto`` is one destination, so one slot.

    This path is deduped further down, by ``scheduling.optimal_slots``, rather
    than by the schema validator the rest of this module is about — the query
    parameter never passes through a model. Pinned here so the two dedupes are
    read as one behaviour: whichever end of the request a repeat arrives at, it
    resolves to a single destination.
    """
    resp = client.get(
        f"/api/v1/content/{approved.id}/schedule/suggestions",
        params=[("platforms", "devto"), ("platforms", "devto")],
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert [slot["platform"] for slot in resp.json()] == ["devto"]


def test_the_callers_order_survives_deduping(client, auth, approved, connected):
    """Order is meaningful — the first destination is the piece's original.

    ``publishing_service.queue`` re-sorts by canonical rank, but the dedupe
    itself must not reorder what it was given, or a caller listing its own
    domain first would find that no longer true.
    """
    from app.schemas.content import unique_platforms

    assert unique_platforms(
        [Platform.MASTODON, Platform.DEVTO, Platform.MASTODON]
    ) == [Platform.MASTODON, Platform.DEVTO]


def test_dedupe_leaves_none_alone():
    """``platforms`` is optional on the schedule payload; ``None`` means "as queued"."""
    from app.schemas.content import unique_platforms

    assert unique_platforms(None) is None
