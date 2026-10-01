"""The trigger HTTP surface, including the one public endpoint Pulse exposes."""
from __future__ import annotations

import json

from app.models.content import Content
from app.models.mixins import utcnow
from app.models.project import AutopilotMode, Project
from app.models.trigger import Trigger, TriggerKind
from app.security import hash_password
from app.services import triggers as trigger_service
from app.services import webhooks

API = "/api/v1/triggers"


def _create(client, auth, project, kind="webhook", **config):
    return client.post(
        API,
        json={
            "project_id": project.id,
            "kind": kind,
            "name": f"{kind} trigger",
            "config": config,
        },
        headers=auth,
    )


def test_kinds_are_listed_with_their_config_requirements(client, auth):
    resp = client.get(f"{API}/kinds", headers=auth)

    assert resp.status_code == 200
    kinds = {k["kind"]: k for k in resp.json()}
    assert set(kinds) == {"github", "webhook", "rss", "schedule"}
    assert kinds["rss"]["required_config"] == ["feed_url"]
    assert "content_type" in kinds["schedule"]["optional_config"]


def test_creating_a_webhook_trigger_returns_the_url_and_the_secret_once(
    client, auth, project, db
):
    resp = _create(client, auth, project)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"]
    assert body["inbound_url"].endswith(f"/triggers/inbound/{_token(db)}")

    # ...and never again.
    listed = client.get(API, headers=auth).json()
    assert "secret" not in listed[0]
    assert listed[0]["has_secret"] is True


def _token(db) -> str:
    return db.query(Trigger).one().token


def test_an_rss_trigger_without_a_feed_url_is_refused(client, auth, project):
    resp = _create(client, auth, project, kind="rss")

    assert resp.status_code == 422
    assert "feed_url" in resp.text


def test_an_rss_trigger_pointed_at_a_private_address_is_refused(client, auth, project):
    resp = _create(client, auth, project, kind="rss", feed_url="http://127.0.0.1/f.xml")

    assert resp.status_code == 422


def test_a_misspelled_config_key_is_refused_rather_than_silently_ignored(
    client, auth, project
):
    """A typo'd setting is a trigger that never fires and never says why."""
    resp = _create(
        client, auth, project, kind="rss", feed_uri="https://example.com/f.xml"
    )

    assert resp.status_code == 422
    assert "feed_uri" in resp.text


def test_an_unknown_content_type_is_refused(client, auth, project):
    resp = _create(
        client,
        auth,
        project,
        kind="schedule",
        content_type="press_release_but_not_yet",
    )

    assert resp.status_code == 422


def test_a_trigger_on_someone_elses_project_is_not_found(client, auth, db, user):
    stranger = Project(
        user_id=user.id + 999, name="Theirs", slug="theirs", description=""
    )
    db.add(stranger)
    db.commit()

    resp = client.post(
        API,
        json={"project_id": stranger.id, "kind": "webhook", "config": {}},
        headers=auth,
    )

    assert resp.status_code == 404


def test_triggers_are_listed_only_for_their_owner(client, auth, project, db):
    _create(client, auth, project)

    other = Trigger(project_id=_foreign_project(db).id, kind=TriggerKind.RSS, config={})
    db.add(other)
    db.commit()

    listed = client.get(API, headers=auth).json()

    assert [t["project_id"] for t in listed] == [project.id]


def _foreign_project(db) -> Project:
    from app.models.user import User

    stranger = User(
        email="other@example.com", full_name="Other", hashed_password=hash_password("x" * 12)
    )
    db.add(stranger)
    db.commit()
    row = Project(user_id=stranger.id, name="Theirs", slug="theirs-2", description="")
    db.add(row)
    db.commit()
    return row


def test_patching_a_config_replaces_it_wholesale(client, auth, project, db):
    created = _create(client, auth, project, kind="schedule", every_hours=24, topic="A").json()

    resp = client.patch(
        f"{API}/{created['id']}", json={"config": {"every_hours": 168}}, headers=auth
    )

    assert resp.status_code == 200
    assert resp.json()["config"] == {"every_hours": 168.0}


def test_reactivating_a_trigger_clears_its_failure_count(client, auth, project, db):
    created = _create(client, auth, project, kind="rss", feed_url="https://example.com/f.xml").json()
    trigger = db.get(Trigger, created["id"])
    trigger.is_active = False
    trigger.consecutive_failures = 19
    trigger.last_error = "gone"
    db.commit()

    resp = client.patch(f"{API}/{created['id']}", json={"is_active": True}, headers=auth)

    assert resp.json()["consecutive_failures"] == 0
    assert resp.json()["last_error"] is None


def test_rotating_a_secret_also_changes_the_url(client, auth, project, db):
    created = _create(client, auth, project).json()

    rotated = client.post(f"{API}/{created['id']}/rotate-secret", headers=auth).json()

    assert rotated["secret"] != created["secret"]
    assert rotated["inbound_url"] != created["inbound_url"]


def test_only_a_webhook_trigger_has_a_secret_to_rotate(client, auth, project):
    created = _create(client, auth, project, kind="schedule").json()

    resp = client.post(f"{API}/{created['id']}/rotate-secret", headers=auth)

    assert resp.status_code == 409


def test_check_now_is_refused_for_an_inbound_webhook(client, auth, project):
    created = _create(client, auth, project).json()

    resp = client.post(f"{API}/{created['id']}/check", headers=auth)

    assert resp.status_code == 409


def test_deleting_a_trigger_takes_its_events_with_it(client, auth, project, db):
    created = _create(client, auth, project).json()
    trigger = db.get(Trigger, created["id"])
    trigger_service.record(
        db, trigger, trigger_service.signal_from_webhook(trigger, {"a": 1})
    )

    assert client.delete(f"{API}/{created['id']}", headers=auth).status_code == 204
    from app.models.trigger import TriggerEvent

    assert db.query(TriggerEvent).count() == 0


# --------------------------------------------------------------------------- #
# The public inbound endpoint                                                  #
# --------------------------------------------------------------------------- #


def test_posting_to_the_inbound_url_writes_a_piece(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created = _create(client, auth, project, headline_path="title").json()
    token = db.get(Trigger, created["id"]).token

    resp = client.post(f"{API}/inbound/{token}", json={"title": "We shipped a thing"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "generated"
    assert db.query(Content).count() == 1


def test_the_inbound_endpoint_needs_no_bearer_token(client, project, db, auth):
    """The sender is a CI job or a zap. It has a URL and no Pulse account."""
    created = _create(client, auth, project).json()
    token = db.get(Trigger, created["id"]).token

    resp = client.post(f"{API}/inbound/{token}", json={"any": "body"})

    assert resp.status_code == 202


def test_an_unknown_token_is_a_flat_404(client):
    resp = client.post(f"{API}/inbound/not-a-real-token", json={})

    assert resp.status_code == 404


def test_a_deactivated_trigger_looks_exactly_like_an_unknown_one(
    client, auth, project, db
):
    created = _create(client, auth, project).json()
    trigger = db.get(Trigger, created["id"])
    token = trigger.token
    trigger.is_active = False
    db.commit()

    resp = client.post(f"{API}/inbound/{token}", json={})

    assert resp.status_code == 404


def test_a_retried_delivery_does_not_write_twice(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created = _create(client, auth, project, dedupe_path="id").json()
    token = db.get(Trigger, created["id"]).token
    body = {"id": "evt-42", "title": "Deployed"}

    first = client.post(f"{API}/inbound/{token}", json=body)
    second = client.post(f"{API}/inbound/{token}", json=body)

    assert first.json()["status"] == "generated"
    assert second.json()["status"] == "duplicate"
    assert db.query(Content).count() == 1


def test_a_non_json_body_is_accepted_as_text(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created = _create(client, auth, project).json()
    token = db.get(Trigger, created["id"]).token

    resp = client.post(
        f"{API}/inbound/{token}",
        content=b"deploy finished: v2.1.0",
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 202
    assert db.query(Content).count() == 1


def test_a_trigger_requiring_a_signature_refuses_an_unsigned_post(
    client, auth, project, db
):
    created = _create(client, auth, project, require_signature=True).json()
    token = db.get(Trigger, created["id"]).token

    resp = client.post(f"{API}/inbound/{token}", json={"title": "hi"})

    assert resp.status_code == 401


def test_a_correctly_signed_post_is_accepted(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created = _create(client, auth, project, require_signature=True)
    secret = created.json()["secret"]
    token = db.get(Trigger, created.json()["id"]).token

    body = json.dumps({"title": "Signed and sealed"})
    signature = webhooks.sign(secret, int(utcnow().timestamp()), body)

    resp = client.post(
        f"{API}/inbound/{token}",
        content=body.encode(),
        headers={
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: signature,
        },
    )

    assert resp.status_code == 202, resp.text
    assert db.query(Content).count() == 1


def test_a_tampered_body_fails_verification(client, auth, project, db):
    created = _create(client, auth, project, require_signature=True)
    secret = created.json()["secret"]
    token = db.get(Trigger, created.json()["id"]).token
    signature = webhooks.sign(secret, int(utcnow().timestamp()), '{"title":"original"}')

    resp = client.post(
        f"{API}/inbound/{token}",
        content=b'{"title":"substituted"}',
        headers={
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: signature,
        },
    )

    assert resp.status_code == 401


def test_the_event_log_shows_what_the_trigger_did(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created = _create(client, auth, project, headline_path="title").json()
    token = db.get(Trigger, created["id"]).token
    client.post(f"{API}/inbound/{token}", json={"title": "Something shipped"})

    resp = client.get(f"{API}/{created['id']}/events", headers=auth)

    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "1"
    event = resp.json()[0]
    assert event["headline"] == "Something shipped"
    assert event["status"] == "generated"
    assert event["content_id"] is not None
