"""What each caller does when a stored secret will not decrypt.

``test_crypto.py`` covers the primitive: rotate the key, get a
``CredentialEncryptionError``. What was untested is every place that *catches*
one — and they deliberately do not agree, because the right answer differs:

* **Platform connections** flip to ``invalid`` and say so on the settings page.
  The credential really is unusable and the user has to re-enter it.
* **Inbound trigger secrets** degrade to ``""`` and the signature check fails
  closed. A crash here would be a 500 on a public endpoint, and an exception
  that escapes is a worse answer than "that signature does not match".
* **Outbound webhook secrets** also degrade to ``""``, and the delivery is
  failed *terminally* with a message naming the fix. Retrying sixteen times
  cannot make an unreadable secret readable.

The single mechanism throughout is a key rotation: encrypt under one Fernet
key, then point settings at another. That is the real-world event these
branches exist for.
"""
from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet

from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.trigger import Trigger, TriggerKind
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services import publishing_service, webhooks
from app.services import triggers as trigger_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import CredentialError


@pytest.fixture
def receiver(monkeypatch):
    """An in-process endpoint, so a delivery that should never go out has
    somewhere to have not gone.
    """

    class _Receiver:
        def __init__(self):
            self.requests: list[httpx.Request] = []

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, text="")

    listener = _Receiver()
    monkeypatch.setattr(
        webhooks,
        "_http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(listener), follow_redirects=False
        ),
    )
    return listener


@pytest.fixture
def key(monkeypatch) -> str:
    """A Fernet key that everything is encrypted under to begin with."""
    value = Fernet.generate_key().decode()
    monkeypatch.setattr("app.services.crypto.settings.token_encryption_key", value)
    return value


@pytest.fixture
def rotate(monkeypatch):
    """Swap in a different key — the rotation these branches exist for."""

    def _rotate():
        monkeypatch.setattr(
            "app.services.crypto.settings.token_encryption_key",
            Fernet.generate_key().decode(),
        )

    return _rotate


# --------------------------------------------------------------------------- #
# Platform connections                                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def connection(db, user, key) -> PlatformConnection:
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@r2st",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_publishing_reads_an_unreadable_credential_as_a_credential_error(
    db, user, connection, rotate
):
    """Not a transient failure. ``CredentialError`` is what stops the publish
    burning its retry budget on something no retry can fix.
    """
    rotate()

    with pytest.raises(CredentialError) as exc:
        publishing_service._credentials_for(db, user.id, Platform.DEVTO)
    assert "TOKEN_ENCRYPTION_KEY" in str(exc.value)


def test_verifying_a_connection_whose_key_rotated_marks_it_invalid(
    client, auth, db, connection, rotate
):
    rotate()

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "invalid"
    assert "TOKEN_ENCRYPTION_KEY" in resp.json()["last_error"]


def test_an_unreachable_platform_does_not_mark_the_connection_invalid(
    client, auth, db, connection, monkeypatch
):
    """The other half of the same handler, and the distinction it exists for.

    A platform that is down is not a credential that is wrong. Flipping to
    ``invalid`` would send the user off to re-enter a token that works.
    """
    from app.services import publishers
    from app.services.publishers.base import PublishError

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter,
        "verify",
        lambda creds: (_ for _ in ()).throw(PublishError("Dev.to is down")),
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "connected"
    assert "Dev.to is down" in resp.json()["last_error"]


def test_a_successful_verify_clears_a_previous_error(
    client, auth, db, connection, monkeypatch
):
    from app.services import publishers

    connection.last_error = "something went wrong last time"
    connection.status = ConnectionStatus.INVALID
    db.commit()
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO), "verify", lambda creds: "@r2st"
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.json()["status"] == "connected"
    assert resp.json()["last_error"] is None


# --------------------------------------------------------------------------- #
# Inbound trigger secrets                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def trigger(db, project, key) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="Inbound",
        is_active=True,
        encrypted_secret=trigger_service.store_secret("shhh-a-secret-value"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_an_unreadable_trigger_secret_reads_as_empty_rather_than_raising(
    trigger, rotate
):
    """This runs inside a public inbound endpoint. An exception escaping here
    is a 500 for anybody who can guess the URL; ``""`` fails the signature
    check closed, which is the same refusal without the stack trace.
    """
    rotate()

    assert trigger_service.read_secret(trigger) == ""


def test_a_trigger_with_no_secret_at_all_reads_as_empty(db, project):
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="No secret",
        is_active=True,
        encrypted_secret="",
    )
    db.add(row)
    db.commit()

    assert trigger_service.read_secret(row) == ""


def test_a_readable_trigger_secret_still_comes_back(trigger):
    assert trigger_service.read_secret(trigger) == "shhh-a-secret-value"


# --------------------------------------------------------------------------- #
# Outbound webhook secrets                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def webhook(db, user, key) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url="https://hooks.example.com/herald",
        description="Slack",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("shhh-a-secret-value"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_an_unreadable_webhook_secret_reads_as_empty_rather_than_raising(
    webhook, rotate
):
    rotate()

    assert webhooks.read_secret(webhook) == ""


def test_a_delivery_signed_with_an_unreadable_secret_fails_terminally(
    db, webhook, rotate
):
    """Terminal, not retried: no number of attempts makes a rotated key
    readable, and sixteen of them is how one bad row becomes a log full of
    noise. The message has to name the fix, because the user cannot infer
    "rotate the secret in Settings" from a delivery that just stopped.
    """
    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"hello": "world"}),
        status=DeliveryStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.commit()
    rotate()

    webhooks.deliver(db, delivery)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.next_attempt_at is None
    assert "secret" in delivery.error.lower()
    assert "Settings" in delivery.error


def test_the_unreadable_secret_never_reaches_the_endpoint(db, webhook, receiver, rotate):
    """It has to fail *before* the request goes out. Posting an unsigned — or
    wrongly signed — payload to a customer's endpoint is worse than not
    delivering it.
    """
    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"hello": "world"}),
        status=DeliveryStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.commit()
    rotate()

    webhooks.deliver(db, delivery)

    assert receiver.requests == []
