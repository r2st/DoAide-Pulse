"""Content HTTP branches that only run on a filter, a refusal, or a batch miss.

These are the arms of endpoints that ``test_content.py`` walks the happy path
of. Each one is a decision the API makes on the caller's behalf — which pieces a
filter keeps, which id in a batch is reported back rather than acted on, and
which requests are refused outright — and none of them was pinned.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.routers import content as content_router
from app.services import github_client
from app.services.crypto import encrypt_credentials

API = "/api/v1/content"


def _content(db, project, *, status=ContentStatus.REVIEW, ctype=ContentType.HOW_TO,
             title="A", slug="a") -> Content:
    row = Content(
        project_id=project.id,
        content_type=ctype,
        status=status,
        title=title,
        slug=slug,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Listing filters                                                             #
# --------------------------------------------------------------------------- #


def test_the_list_can_be_filtered_to_one_content_type(client, auth, project, db):
    how_to = _content(db, project, ctype=ContentType.HOW_TO, title="H", slug="h")
    _content(
        db, project, ctype=ContentType.ANNOUNCEMENT, title="A", slug="ann"
    )

    resp = client.get(API, params={"content_type": "how_to"}, headers=auth)

    assert resp.status_code == 200, resp.text
    assert [c["id"] for c in resp.json()] == [how_to.id]
    assert resp.headers["X-Total-Count"] == "1"


# --------------------------------------------------------------------------- #
# Batch misses                                                                #
# --------------------------------------------------------------------------- #


def test_bulk_reject_reports_an_already_published_piece_instead_of_archiving_it(
    client, auth, project, db
):
    """Archiving something that is already live would hide a post that exists."""
    reviewable = _content(db, project, title="A", slug="a")
    live = _content(
        db, project, status=ContentStatus.PUBLISHED, title="B", slug="b"
    )

    resp = client.post(
        f"{API}/bulk/reject",
        headers=auth,
        json={"content_ids": [reviewable.id, live.id]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == [reviewable.id]
    assert body["failed"] == [
        {"content_id": live.id, "reason": "Already published"}
    ]
    db.refresh(live)
    assert live.status == ContentStatus.PUBLISHED


def test_bulk_publish_reports_an_id_that_is_not_yours_as_not_found(
    client, auth, project, db
):
    mine = _content(db, project, status=ContentStatus.APPROVED)

    resp = client.post(
        f"{API}/bulk/publish",
        headers=auth,
        json={"content_ids": [mine.id, 999999], "platforms": ["devto"]},
    )

    assert resp.status_code == 200, resp.text
    failed = resp.json()["failed"]
    assert {f["content_id"] for f in failed} >= {999999}
    assert next(f for f in failed if f["content_id"] == 999999)["reason"] == "Not found"


def test_the_ownership_map_short_circuits_on_an_empty_batch(db, user):
    """No ids means no query — the schemas keep HTTP callers off this path."""
    assert content_router._owned_content_map([], db, user) == {}


# --------------------------------------------------------------------------- #
# Refusals                                                                    #
# --------------------------------------------------------------------------- #


def test_approving_something_that_already_went_out_is_refused(
    client, auth, project, db
):
    live = _content(db, project, status=ContentStatus.PUBLISHED)

    resp = client.post(f"{API}/{live.id}/approve", headers=auth)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "This piece is already published — approving it again has no effect."


def test_slot_suggestions_need_somewhere_to_suggest_slots_for(
    client, auth, project, db
):
    """Nothing queued and nothing connected — there is no question to answer."""
    orphan = _content(db, project)

    resp = client.get(f"{API}/{orphan.id}/schedule/suggestions", headers=auth)

    assert resp.status_code == 400
    assert "no platform is connected" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Degraded upstreams                                                          #
# --------------------------------------------------------------------------- #


def test_a_github_outage_thins_the_draft_rather_than_failing_the_request(
    client, auth, project, monkeypatch
):
    assert project.repo_full_name == "r2st/DoAide-Pulse"

    def unavailable(*_args, **_kwargs):
        raise github_client.GitHubError("502 Bad Gateway")

    monkeypatch.setattr(content_router.github_client, "fetch_activity", unavailable)

    resp = client.post(
        f"{API}/generate",
        headers=auth,
        json={
            "project_id": project.id,
            "content_type": "changelog",
            "include_repo_activity": True,
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["title"]
    assert resp.json()["source"]["fallback"] is True


# --------------------------------------------------------------------------- #
# Rescheduling                                                                #
# --------------------------------------------------------------------------- #


def test_rescheduling_leaves_the_platform_it_already_went_out_on_alone(
    client, auth, project, user, db
):
    """A published row has no future to move; rewriting it would post twice."""
    for platform in (Platform.DEVTO, Platform.MEDIUM):
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                status=ConnectionStatus.CONNECTED,
                encrypted_credentials=encrypt_credentials({"api_key": "k"}),
                display_name="@dev",
            )
        )
    db.commit()
    piece = _content(db, project, status=ContentStatus.APPROVED)
    published = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=datetime.now(UTC) - timedelta(days=1),
        external_url="https://dev.to/x",
    )
    pending = Publication(
        content_id=piece.id,
        platform=Platform.MEDIUM,
        status=PublicationStatus.PENDING,
    )
    db.add_all([published, pending])
    db.commit()
    db.refresh(published)
    db.refresh(pending)
    was = published.published_at

    resp = client.post(
        f"{API}/{piece.id}/schedule", headers=auth, json={"optimize": True}
    )

    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Publication, published.id).status == PublicationStatus.PUBLISHED
    assert db.get(Publication, published.id).published_at == was
    assert db.get(Publication, pending.id).status == PublicationStatus.SCHEDULED
    assert db.get(Publication, pending.id).scheduled_for is not None
