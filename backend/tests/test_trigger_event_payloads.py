"""The firing history stopped shipping every payload it had ever stored.

A ``TriggerEvent``'s payload is the frozen signal, and for an inbound webhook
that includes ``raw`` — the sender's body, verbatim, bounded only by
``MAX_INBOUND_BYTES`` at 128 KB. ``GET /triggers/{id}/events`` pages up to 200 of
them, so the activity list was a response measured in tens of megabytes to
render a column of one-line rows that has never read a payload at all.

So the listing leaves them out unless asked, and a single event can be fetched
on its own for the case where somebody actually wants one. The tests here are in
three groups: the listing's new default, the way back to the old shape, and the
new endpoint — including the pair-shaped ownership check that a nested id needs
and the sweep in ``test_tenant_isolation`` also covers.
"""
from __future__ import annotations

from app.models.project import AutopilotMode
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus

API = "/api/v1/triggers"


def _webhook_trigger(client, auth, project) -> dict:
    return client.post(
        API,
        json={
            "project_id": project.id,
            "kind": "webhook",
            "name": "deploys",
            "config": {"headline_path": "title"},
        },
        headers=auth,
    ).json()


def _fire(client, db, trigger_id: int, body: dict) -> None:
    token = db.get(Trigger, trigger_id).token
    resp = client.post(f"{API}/inbound/{token}", json=body)
    assert resp.status_code == 202, resp.text


# --------------------------------------------------------------------------- #
# The listing                                                                  #
# --------------------------------------------------------------------------- #


def test_the_listing_omits_payloads_by_default(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    trigger = _webhook_trigger(client, auth, project)
    _fire(client, db, trigger["id"], {"title": "Shipped", "secret_field": "x" * 5000})

    resp = client.get(f"{API}/{trigger['id']}/events", headers=auth)

    assert resp.status_code == 200, resp.text
    event = resp.json()[0]
    # Null, not {}: an empty payload is a real state, and the two must not read
    # the same to a client deciding whether to go and fetch one.
    assert event["payload"] is None
    # Everything the activity list actually renders is still here.
    assert event["headline"] == "Shipped"
    assert event["status"] == "generated"
    assert event["content_id"] is not None
    assert resp.headers["X-Total-Count"] == "1"


def test_the_omitted_payload_never_leaves_the_database(
    client, auth, project, db, sql_log
):
    """The point of the change, which the response body alone cannot show.

    Dropping the column after selecting it would produce exactly the JSON the
    test above asserts on, having already carried every byte across the wire.
    Two things are pinned instead: no statement selects the column, and no
    statement is issued *per row* to undefer it afterwards — which is what
    ``TriggerEventOut.model_validate`` on a deferred row would do, turning one
    query into 200.
    """
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    trigger = _webhook_trigger(client, auth, project)
    for i in range(3):
        _fire(client, db, trigger["id"], {"title": f"Shipped {i}", "id": i})
    sql_log.clear()

    resp = client.get(f"{API}/{trigger['id']}/events", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["payload"] for row in resp.json()] == [None, None, None]
    carrying = [s for s in sql_log if "trigger_events.payload" in s]
    assert carrying == [], "\n".join(s[:300] for s in carrying)


def test_asking_for_payloads_gets_them(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    trigger = _webhook_trigger(client, auth, project)
    _fire(client, db, trigger["id"], {"title": "Shipped", "version": "1.2.0"})

    resp = client.get(
        f"{API}/{trigger['id']}/events",
        params={"include_payload": "true"},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()[0]["payload"]
    assert payload["headline"] == "Shipped"
    assert payload["raw"] == {"title": "Shipped", "version": "1.2.0"}


def test_a_firing_that_carried_nothing_says_so_rather_than_reading_as_omitted(
    client, auth, project, db
):
    """``{}`` and ``null`` have to stay distinguishable, so this pins the empty one."""
    trigger = _webhook_trigger(client, auth, project)
    db.add(
        TriggerEvent(
            trigger_id=trigger["id"],
            headline="Nothing at all",
            payload={},
            status=TriggerEventStatus.SKIPPED,
        )
    )
    db.commit()

    resp = client.get(
        f"{API}/{trigger['id']}/events",
        params={"include_payload": "true"},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["payload"] == {}


def test_the_status_filter_and_paging_still_work_alongside_the_new_flag(
    client, auth, project, db
):
    trigger = _webhook_trigger(client, auth, project)
    db.add_all(
        [
            TriggerEvent(
                trigger_id=trigger["id"],
                headline=f"Event {i}",
                payload={"n": i},
                status=(
                    TriggerEventStatus.SKIPPED if i % 2 else TriggerEventStatus.FAILED
                ),
            )
            for i in range(4)
        ]
    )
    db.commit()

    resp = client.get(
        f"{API}/{trigger['id']}/events",
        params={"status": "skipped", "limit": 1, "include_payload": "true"},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Total-Count"] == "2"
    assert [row["headline"] for row in resp.json()] == ["Event 3"]
    assert resp.json()[0]["payload"] == {"n": 3}


# --------------------------------------------------------------------------- #
# One event on its own                                                         #
# --------------------------------------------------------------------------- #


def test_one_event_comes_back_with_its_payload(client, auth, project, db):
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    trigger = _webhook_trigger(client, auth, project)
    _fire(client, db, trigger["id"], {"title": "Shipped", "version": "1.2.0"})
    event_id = client.get(f"{API}/{trigger['id']}/events", headers=auth).json()[0]["id"]

    resp = client.get(f"{API}/{trigger['id']}/events/{event_id}", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == event_id
    assert body["headline"] == "Shipped"
    assert body["payload"]["raw"] == {"title": "Shipped", "version": "1.2.0"}


def test_an_unknown_event_id_is_a_404(client, auth, project, db):
    trigger = _webhook_trigger(client, auth, project)

    resp = client.get(f"{API}/{trigger['id']}/events/999999", headers=auth)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trigger event not found"


def test_an_event_belonging_to_another_trigger_of_your_own_is_a_404(
    client, auth, project, db
):
    """The pair check: owning the trigger in the path is not enough.

    Looking the event up by primary key alone would let any id in the table be
    read through a trigger the caller does own — which is the whole account's
    firing history, and for an inbound webhook that means every body anyone has
    ever POSTed to it. Both accounts' rows are covered by the sweep in
    ``test_tenant_isolation``; this is the same-account half, which that sweep
    cannot express.
    """
    first = _webhook_trigger(client, auth, project)
    second = client.post(
        API,
        json={
            "project_id": project.id,
            "kind": "schedule",
            "name": "weekly",
            "config": {"topic": "Roundup"},
        },
        headers=auth,
    ).json()
    db.add(
        TriggerEvent(
            trigger_id=second["id"],
            headline="The other trigger's firing",
            payload={"raw": {"authorization": "a header somebody sent"}},
        )
    )
    db.commit()
    stolen = client.get(f"{API}/{second['id']}/events", headers=auth).json()[0]["id"]

    resp = client.get(f"{API}/{first['id']}/events/{stolen}", headers=auth)

    assert resp.status_code == 404
    # And it is reachable under the trigger it does belong to, so the 404 above
    # is the guard rather than a row that was never there.
    assert (
        client.get(f"{API}/{second['id']}/events/{stolen}", headers=auth).status_code
        == 200
    )
