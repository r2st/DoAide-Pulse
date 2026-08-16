"""A dry run has to be a preview of *this* call, not of a similar one.

The four ``/content/bulk/*`` endpoints act on up to a hundred pieces at a time,
and their failures are the ones a caller cannot predict from the list they are
looking at: a platform they have not connected, an adapter that is not finished,
a piece somebody else archived this morning. Half a batch queued is half a batch
that has to be found and unqueued one publication at a time.

So each of them takes ``dry_run``. The property that makes it worth anything is
not "it changes nothing" — that is the easy half, and it is checked below. It is
that the verdicts are *the same verdicts*: the dry run and the real call share
one validator (``_assert_publishable``) rather than each having its own idea of
what would work. A preview built from a re-implementation drifts, and the moment
it drifts is the moment somebody trusts it.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services.crypto import encrypt_credentials


def _piece(db, project, n: int, status: ContentStatus = ContentStatus.REVIEW) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Piece {n}",
        slug=f"piece-{n}",
        body_markdown="A body with no links in it at all.",
        excerpt="A body.",
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def three(db, project) -> list[Content]:
    return [_piece(db, project, n) for n in range(3)]


@pytest.fixture
def devto(db, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()


# --------------------------------------------------------------------------- #
# It changes nothing                                                           #
# --------------------------------------------------------------------------- #


def test_a_dry_run_approve_leaves_every_status_alone(client, auth, db, three):
    ids = [c.id for c in three]
    resp = client.post(
        "/api/v1/content/bulk/approve",
        json={"content_ids": ids, "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["succeeded"]) == sorted(ids)
    db.expire_all()
    assert [db.get(Content, i).status for i in ids] == [ContentStatus.REVIEW] * 3


def test_a_dry_run_reject_archives_nothing(client, auth, db, three):
    ids = [c.id for c in three]
    resp = client.post(
        "/api/v1/content/bulk/reject",
        json={"content_ids": ids, "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["succeeded"]) == sorted(ids)
    db.expire_all()
    assert not [i for i in ids if db.get(Content, i).status == ContentStatus.ARCHIVED]


def test_a_dry_run_publish_queues_nothing(client, auth, db, three, devto):
    ids = [c.id for c in three]
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={"content_ids": ids, "platforms": ["devto"], "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["succeeded"]) == sorted(ids)
    db.expire_all()
    assert db.query(Publication).count() == 0
    # And the pieces are still in review — the real call promotes them to
    # approved on its way past, which is exactly the kind of side effect a
    # preview must not have.
    assert [db.get(Content, i).status for i in ids] == [ContentStatus.REVIEW] * 3


def test_a_dry_run_retry_neither_rearms_nor_dispatches(
    client, auth, db, three, devto, monkeypatch
):
    """The dispatch is the half that cannot be taken back.

    A re-armed row is a row somebody can cancel; a task already handed to a
    worker is a request on its way to a platform.
    """
    dispatched: list[int] = []
    from app.routers import content as content_router

    monkeypatch.setattr(
        content_router, "_dispatch", lambda ids: dispatched.extend(ids)
    )

    for piece in three:
        db.add(
            Publication(
                content_id=piece.id,
                platform=Platform.DEVTO,
                status=PublicationStatus.FAILED,
                attempts=2,
                error="the platform said no",
            )
        )
    db.commit()

    ids = [c.id for c in three]
    resp = client.post(
        "/api/v1/content/bulk/retry",
        json={"content_ids": ids, "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["succeeded"]) == sorted(ids)
    assert dispatched == []
    db.expire_all()
    rows = db.query(Publication).all()
    assert {r.status for r in rows} == {PublicationStatus.FAILED}
    assert {r.attempts for r in rows} == {2}


# --------------------------------------------------------------------------- #
# It says the same thing the real call would                                   #
# --------------------------------------------------------------------------- #


def test_the_dry_run_and_the_real_call_agree_on_a_mixed_batch(
    client, auth, db, project, three, devto
):
    """The property the whole feature rests on.

    A batch of four: two publishable, one archived, one that does not exist.
    The preview and the real call must sort them the same way and give the same
    reasons, because they run the same validator.
    """
    archived = _piece(db, project, 9, ContentStatus.ARCHIVED)
    ids = [three[0].id, archived.id, three[1].id, 999_999]

    body = {"content_ids": ids, "platforms": ["devto"]}
    preview = client.post(
        "/api/v1/content/bulk/publish", json={**body, "dry_run": True}, headers=auth
    ).json()
    real = client.post("/api/v1/content/bulk/publish", json=body, headers=auth).json()

    assert preview["succeeded"] == real["succeeded"] == [three[0].id, three[1].id]
    assert [f["reason"] for f in preview["failed"]] == [
        f["reason"] for f in real["failed"]
    ]
    assert {f["content_id"] for f in preview["failed"]} == {archived.id, 999_999}
    assert preview["dry_run"] is True
    assert real["dry_run"] is False


def test_a_dry_run_reports_the_platform_that_is_not_connected(
    client, auth, three
):
    """No connection fixture here, so every piece fails the same way — and this
    is the failure a caller most wants told before rather than after."""
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={
            "content_ids": [c.id for c in three],
            "platforms": ["devto"],
            "dry_run": True,
        },
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == []
    assert all("Not connected" in f["reason"] for f in resp.json()["failed"])


def test_a_dry_run_publish_refuses_a_bad_zone_per_piece(client, auth, three, devto):
    """A whole-request mistake still comes back as a per-piece verdict, because
    that is the shape every other failure in this endpoint has."""
    resp = client.post(
        "/api/v1/content/bulk/publish",
        json={
            "content_ids": [c.id for c in three],
            "platforms": ["devto"],
            "scheduled_for": "2026-11-05T09:00:00",
            "timezone": "Europe/Nowhere",
            "dry_run": True,
        },
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == []
    assert all("Europe/Nowhere" in f["reason"] for f in resp.json()["failed"])


def test_a_dry_run_retry_still_reports_nothing_to_retry(client, auth, db, three):
    """A piece with no failed publication is a *failure* in the result, in the
    preview exactly as in the real call: a caller told "succeeded" for a piece
    whose only publication is live has been told its retry happened."""
    resp = client.post(
        "/api/v1/content/bulk/retry",
        json={"content_ids": [c.id for c in three], "dry_run": True},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == []
    assert all(f["reason"] == "Nothing to retry" for f in resp.json()["failed"])


# --------------------------------------------------------------------------- #
# The counter a progress bar is drawn against                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path, extra",
    [
        ("approve", {}),
        ("reject", {}),
        ("retry", {}),
        ("publish", {"platforms": ["devto"]}),
    ],
)
def test_every_bulk_result_accounts_for_every_id_it_was_given(
    client, auth, three, path, extra
):
    """``total`` is ``len(succeeded) + len(failed)``, and both partition the
    input. Sent rather than left to the client to add up, because a client that
    computes it has to know the two lists partition the input — which is true,
    and is exactly the kind of invariant that stops being true one refactor
    later."""
    ids = [*[c.id for c in three], 999_999]
    resp = client.post(
        f"/api/v1/content/bulk/{path}",
        json={"content_ids": ids, "dry_run": True, **extra},
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(ids)
    assert body["total"] == len(body["succeeded"]) + len(body["failed"])
    seen = {*body["succeeded"], *(f["content_id"] for f in body["failed"])}
    assert seen == set(ids)


def test_the_real_call_carries_the_counter_too(client, auth, three):
    """``dry_run`` is echoed false rather than omitted: the two bodies are
    otherwise identical, which is the point of a dry run and also the way to
    misread one out of a log."""
    resp = client.post(
        "/api/v1/content/bulk/approve",
        json={"content_ids": [c.id for c in three]},
        headers=auth,
    )
    assert resp.json()["total"] == 3
    assert resp.json()["dry_run"] is False


def test_dry_run_defaults_to_off(client, auth, db, three):
    """The default has to be the destructive one, because that is what every
    existing client sends. A batch endpoint that quietly stopped writing would
    be the worst possible way to add this."""
    resp = client.post(
        "/api/v1/content/bulk/approve",
        json={"content_ids": [c.id for c in three]},
        headers=auth,
    )
    assert resp.status_code == 200
    db.expire_all()
    assert [db.get(Content, c.id).status for c in three] == [
        ContentStatus.APPROVED
    ] * 3
