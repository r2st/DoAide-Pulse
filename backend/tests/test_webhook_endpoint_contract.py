"""The webhook management endpoints, as a contract.

tests/test_webhooks.py is mostly about *delivery* — signatures, retries,
backoff, deactivation. This module is about the CRUD surface a user actually
touches: what a PATCH may change, what the per-account ceiling refuses, and
what happens when the box has no encryption key to store a secret under.

The PATCH is the interesting one. Every field is optional, so each has its own
"was it sent?" branch, and a partial update that quietly wipes an unsent field
is the classic way to lose a subscription list.
"""
from __future__ import annotations

import pytest

from app.models.webhook import Webhook, WebhookEvent
from app.routers.webhooks import MAX_WEBHOOKS_PER_USER
from app.services import link_check, webhooks
from app.services.crypto import CredentialEncryptionError

_URL = "https://hooks.example.com/pulse"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Every hostname is public unless a test says otherwise — otherwise the
    URL guard would answer differently on a laptop and in CI."""
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


@pytest.fixture
def webhook(db, user) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url=_URL,
        description="Slack",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("shhh-a-secret-value"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Creation                                                                    #
# --------------------------------------------------------------------------- #


def test_a_created_webhook_reports_201_and_its_secret_once(client, auth):
    resp = client.post(
        "/api/v1/webhooks",
        headers=auth,
        json={
            "url": _URL,
            "events": [WebhookEvent.CONTENT_PUBLISHED.value],
            "description": "Slack",
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["secret"]
    assert body["url"] == _URL
    assert body["is_active"] is True


def test_the_twenty_first_webhook_is_refused_with_a_409(client, auth, db, user):
    """A ceiling per account, not per request.

    Every emitted event fans out to every subscribed endpoint, so an unbounded
    list turns one publish into an unbounded number of outbound requests — the
    account becomes its own amplifier.
    """
    for i in range(MAX_WEBHOOKS_PER_USER):
        db.add(
            Webhook(
                user_id=user.id,
                url=f"https://hooks.example.com/h{i}",
                events=[WebhookEvent.CONTENT_PUBLISHED.value],
                encrypted_secret=webhooks.store_secret(f"secret-number-{i}"),
            )
        )
    db.commit()

    resp = client.post(
        "/api/v1/webhooks",
        headers=auth,
        json={"url": _URL, "events": [WebhookEvent.CONTENT_PUBLISHED.value]},
    )

    assert resp.status_code == 409
    assert str(MAX_WEBHOOKS_PER_USER) in resp.json()["detail"]


def test_the_ceiling_counts_only_this_users_endpoints(client, auth, db, user):
    """Another account filling its own quota must not close this one's."""
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.flush()
    for i in range(MAX_WEBHOOKS_PER_USER):
        db.add(
            Webhook(
                user_id=other.id,
                url=f"https://hooks.example.com/other{i}",
                events=[WebhookEvent.CONTENT_PUBLISHED.value],
                encrypted_secret=webhooks.store_secret(f"other-secret-{i}"),
            )
        )
    db.commit()

    resp = client.post(
        "/api/v1/webhooks",
        headers=auth,
        json={"url": _URL, "events": [WebhookEvent.CONTENT_PUBLISHED.value]},
    )

    assert resp.status_code == 201, resp.text


def test_a_box_with_no_encryption_key_refuses_rather_than_storing_plaintext(
    client, auth, monkeypatch
):
    """Production without ``TOKEN_ENCRYPTION_KEY``.

    The alternative — storing the signing secret unencrypted — would make every
    delivery signature forgeable by anyone with read access to the database. A
    500 that says why is the better failure.
    """
    def _boom(_secret: str) -> str:
        raise CredentialEncryptionError("TOKEN_ENCRYPTION_KEY is not set")

    monkeypatch.setattr(webhooks, "store_secret", _boom)

    resp = client.post(
        "/api/v1/webhooks",
        headers=auth,
        json={"url": _URL, "events": [WebhookEvent.CONTENT_PUBLISHED.value]},
    )

    assert resp.status_code == 500
    assert "TOKEN_ENCRYPTION_KEY" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Partial update                                                              #
# --------------------------------------------------------------------------- #


def test_patching_the_url_leaves_everything_else_alone(client, auth, db, webhook):
    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}",
        headers=auth,
        json={"url": "https://hooks.example.com/moved"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"] == "https://hooks.example.com/moved"
    assert body["events"] == [WebhookEvent.CONTENT_PUBLISHED.value]
    assert body["description"] == "Slack"
    assert body["is_active"] is True


def test_patching_the_events_replaces_the_subscription_list(client, auth, webhook):
    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}",
        headers=auth,
        json={
            "events": [
                WebhookEvent.PUBLICATION_FAILED.value,
                WebhookEvent.CONTENT_PUBLISHED.value,
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert set(resp.json()["events"]) == {
        WebhookEvent.PUBLICATION_FAILED.value,
        WebhookEvent.CONTENT_PUBLISHED.value,
    }
    assert resp.json()["url"] == _URL


def test_patching_the_description_trims_it(client, auth, webhook):
    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}",
        headers=auth,
        json={"description": "  Ops channel  "},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Ops channel"


def test_an_empty_patch_changes_nothing(client, auth, webhook):
    """``{}`` is a legal PATCH. Every field has to read as "not sent" rather
    than "sent as null", or a no-op request wipes the subscription list."""
    resp = client.patch(f"/api/v1/webhooks/{webhook.id}", headers=auth, json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"] == _URL
    assert body["events"] == [WebhookEvent.CONTENT_PUBLISHED.value]
    assert body["description"] == "Slack"


def test_patching_a_url_into_the_private_network_is_refused(
    client, auth, webhook, monkeypatch
):
    """The SSRF guard belongs on the update path too — otherwise an endpoint
    created against a public host is edited into ``169.254.169.254`` and the
    delivery worker becomes a metadata-service proxy."""
    monkeypatch.setattr(
        link_check,
        "_unreachable_for_a_reader",
        lambda url: "resolves to a private address",
    )

    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}",
        headers=auth,
        json={"url": "http://169.254.169.254/latest/meta-data/"},
    )

    assert resp.status_code == 422


def test_deactivating_leaves_the_failure_counter_where_it_was(
    client, auth, db, webhook
):
    """Only *re*-activating clears the run. Switching an endpoint off while it
    is failing and back on later is the user saying they fixed it; switching it
    off is not."""
    webhook.consecutive_failures = 4
    db.commit()

    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}", headers=auth, json={"is_active": False}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    assert resp.json()["consecutive_failures"] == 4


def test_patching_another_users_webhook_is_a_404(client, auth, db):
    from app.models.user import User
    from app.security import hash_password

    other = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.flush()
    theirs = Webhook(
        user_id=other.id,
        url="https://hooks.example.com/theirs",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("not-yours-at-all"),
    )
    db.add(theirs)
    db.commit()
    db.refresh(theirs)

    resp = client.patch(
        f"/api/v1/webhooks/{theirs.id}", headers=auth, json={"is_active": False}
    )

    assert resp.status_code == 404
