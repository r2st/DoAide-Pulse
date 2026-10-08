"""Nothing a third party says gets to be an unbounded row.

Every ``last_error`` and ``error`` column in this schema is ``Text`` — no limit
in the database — and every string written into one comes from outside: a
platform quoting our payload back, an RSS host answering with an error page, a
webhook endpoint's HTML. All of them are rewritten on *every* failed attempt and
rendered straight into a list view, so one misbehaving remote end is a bad row,
a slow page and a table that grows on a schedule.

``publishing_service`` clipped at the point of write from the start (see
``test_publishing_service.py``). The other three paths did not, and each is
reachable without a platform being involved at all:

* re-verifying a platform connection from Settings,
* polling an RSS or GitHub trigger,
* an outbound webhook delivery that fails below the HTTP layer.

The bound is deliberately generous — :data:`app.services.errors.MAX_ERROR_CHARS`
— because truncating to something short is worse at the one job these fields
have. What matters is that it exists, and that the cut is visible.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.trigger import Trigger, TriggerKind
from app.models.webhook import Webhook, WebhookDelivery, WebhookEvent
from app.services import errors, publishers, webhooks
from app.services import triggers as trigger_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import CredentialError, PublishError

#: Longer than any real message and far past the bound, so "it was clipped" and
#: "the platform happened to be terse" cannot be confused.
HUGE = "x" * 50_000


def test_the_bound_is_a_bound():
    assert errors.clip_error("short") == "short"
    assert len(errors.clip_error(HUGE)) == errors.MAX_ERROR_CHARS
    assert errors.clip_error(HUGE).endswith("…")


def test_clipping_at_exactly_the_limit_does_not_add_an_ellipsis():
    """Off-by-one guard: a message that fits is not "nearly too long"."""
    exact = "y" * errors.MAX_ERROR_CHARS

    assert errors.clip_error(exact) == exact
    assert not errors.clip_error(exact).endswith("…")


def test_the_clip_lands_on_a_clean_edge():
    """Trailing whitespace before the ellipsis reads as a rendering bug."""
    clipped = errors.clip_error("word " * 20_000)

    assert not clipped[:-1].endswith(" ")
    assert clipped.endswith("…")


# --------------------------------------------------------------------------- #
# Platform connections — re-verify from Settings                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def connection(db, user) -> PlatformConnection:
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


def test_a_vast_credential_error_is_clipped_on_the_connection(
    client, auth, db, connection, monkeypatch
):
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "verify",
        lambda creds: (_ for _ in ()).throw(CredentialError(HUGE)),
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "invalid"
    assert len(resp.json()["last_error"]) == errors.MAX_ERROR_CHARS
    assert resp.json()["last_error"].endswith("…")


def test_a_vast_platform_error_is_clipped_on_the_connection(
    client, auth, db, connection, monkeypatch
):
    """The other branch of the same handler. Both write the column, so
    clipping one and not the other would leave the hole open.
    """
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "verify",
        lambda creds: (_ for _ in ()).throw(PublishError(HUGE)),
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["last_error"]) == errors.MAX_ERROR_CHARS


def test_an_ordinary_connection_error_is_stored_whole(
    client, auth, db, connection, monkeypatch
):
    """Clipping is a ceiling, not a policy. A real message is what the user
    needs, and shortening it would be the fix being worse than the bug.
    """
    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "verify",
        lambda creds: (_ for _ in ()).throw(CredentialError("That API key is not valid.")),
    )

    resp = client.post("/api/v1/settings/connections/devto/verify", headers=auth)

    assert resp.json()["last_error"] == "That API key is not valid."


# --------------------------------------------------------------------------- #
# Triggers — polling a source that answers with a wall of text                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def rss_trigger(db, project) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.RSS,
        name="Upstream releases",
        config={"feed_url": "https://feed.test/rss"},
        state={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_vast_feed_error_is_clipped_on_the_trigger(db, rss_trigger, monkeypatch):
    """A feed host that answers a request with its whole error page puts that
    page in the exception message, and this row is rewritten on every poll.
    """
    monkeypatch.setattr(
        trigger_service.feeds,
        "fetch",
        lambda url: (_ for _ in ()).throw(trigger_service.feeds.FeedError(HUGE)),
    )

    result = trigger_service.check(db, rss_trigger)

    assert result["status"] == "error"
    db.refresh(rss_trigger)
    assert len(rss_trigger.last_error) == errors.MAX_ERROR_CHARS
    assert rss_trigger.last_error.endswith("…")


def test_an_ordinary_feed_error_is_stored_whole(db, rss_trigger, monkeypatch):
    monkeypatch.setattr(
        trigger_service.feeds,
        "fetch",
        lambda url: (_ for _ in ()).throw(trigger_service.feeds.FeedError("404 Not Found")),
    )

    trigger_service.check(db, rss_trigger)

    db.refresh(rss_trigger)
    assert rss_trigger.last_error == "404 Not Found"


def test_a_successful_check_clears_the_error_rather_than_clipping_a_none(
    db, rss_trigger, monkeypatch
):
    """``_mark_checked`` is called with ``None`` on the way through the happy
    path. Clipping that would be a ``TypeError`` on every successful poll.
    """
    rss_trigger.last_error = "it was broken before"
    rss_trigger.consecutive_failures = 2
    db.commit()

    class _Feed:
        title = "Upstream"
        entries: list = []

    monkeypatch.setattr(trigger_service.feeds, "fetch", lambda url: _Feed())
    monkeypatch.setattr(trigger_service.feeds, "new_entries", lambda feed, seen: [])
    monkeypatch.setattr(trigger_service.feeds, "remember", lambda seen, feed: [])

    trigger_service.check(db, rss_trigger)

    db.refresh(rss_trigger)
    assert rss_trigger.last_error is None
    assert rss_trigger.consecutive_failures == 0


# --------------------------------------------------------------------------- #
# Webhooks — a failure below the HTTP layer                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def webhook(db, user) -> Webhook:
    row = Webhook(
        user_id=user.id,
        url="https://hooks.example.com/pulse",
        description="Slack",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("shhh-a-secret-value"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def delivery(db, webhook) -> WebhookDelivery:
    row = WebhookDelivery(
        webhook_id=webhook.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload=webhooks.envelope(WebhookEvent.CONTENT_PUBLISHED, {"hello": "world"}),
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_vast_transport_error_becomes_a_friendly_message(
    db, webhook, delivery, monkeypatch
):
    """A huge raw httpx message is replaced by friendly_network_error, not stored raw."""

    def _explode(request):
        raise httpx.ConnectError(HUGE)

    monkeypatch.setattr(
        webhooks,
        "_http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(_explode), follow_redirects=False
        ),
    )

    webhooks.deliver(db, delivery)

    db.refresh(delivery)
    db.refresh(webhook)
    assert delivery.error == "Could not connect to the server"
    assert webhook.last_error == "Could not connect to the server"
    assert len(delivery.error) < errors.MAX_ERROR_CHARS


def test_an_ordinary_transport_error_is_stored_as_a_friendly_message(
    db, webhook, delivery, monkeypatch
):
    def _explode(request):
        raise httpx.ConnectError("Name or service not known")

    monkeypatch.setattr(
        webhooks,
        "_http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(_explode), follow_redirects=False
        ),
    )

    webhooks.deliver(db, delivery)

    db.refresh(webhook)
    assert webhook.last_error == "Could not connect to the server"
    assert "…" not in webhook.last_error
