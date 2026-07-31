"""Outbound webhooks: subscription, signing, delivery, retry and refusal.

The network is stubbed with ``httpx.MockTransport`` and DNS is stubbed out, so
what is under test is Herald's own decision-making — which endpoint hears about
what, what the body says, what counts as a failure worth retrying, and which
URLs the server refuses to open at all.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware, utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.services import link_check, webhooks

_URL = "https://hooks.example.test/herald"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Every hostname is public unless a test says otherwise.

    Without this the URL guard would depend on the machine's resolver, and the
    suite would pass or fail differently on a laptop and in CI.
    """
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


def _no_close(session):
    """The test session, wrapped so a task's ``db.close()`` does not end it."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture(autouse=True)
def _inline_worker(db, monkeypatch):
    """With Celery off, ``dispatch`` delivers in-process — on the test database.

    The task opens its own session, which would otherwise point at the real
    engine and find no tables at all.
    """
    from app.tasks import webhook_tasks

    monkeypatch.setattr(webhook_tasks, "SessionLocal", _no_close(db))


class _Endpoint:
    """A fake receiver. Records what arrived and answers however it is told."""

    def __init__(self, *, status: int = 200, error: Exception | None = None):
        self.status = status
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status)

    @property
    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content.decode())

    @property
    def last_headers(self) -> httpx.Headers:
        return self.requests[-1].headers


@pytest.fixture
def endpoint(monkeypatch) -> _Endpoint:
    """Point every delivery at an in-process receiver."""
    receiver = _Endpoint()

    def _client() -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(receiver), follow_redirects=False
        )

    monkeypatch.setattr(webhooks, "_http_client", _client)
    return receiver


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


def _delivery(db, webhook: Webhook, event=WebhookEvent.CONTENT_PUBLISHED) -> WebhookDelivery:
    row = WebhookDelivery(
        webhook_id=webhook.id,
        event=event,
        payload=webhooks.envelope(event, {"hello": "world"}),
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Signing                                                                      #
# --------------------------------------------------------------------------- #


def test_a_signature_verifies_against_the_body_it_was_made_for():
    body = '{"event":"content.published"}'
    header = webhooks.sign("secret", int(utcnow().timestamp()), body)
    assert webhooks.verify("secret", header, body)


def test_a_signature_does_not_verify_against_a_different_body():
    header = webhooks.sign("secret", int(utcnow().timestamp()), "original")
    assert not webhooks.verify("secret", header, "tampered")


def test_a_signature_does_not_verify_under_a_different_secret():
    body = "payload"
    header = webhooks.sign("secret", int(utcnow().timestamp()), body)
    assert not webhooks.verify("another-secret", header, body)


def test_an_old_signature_is_refused_even_though_the_hmac_is_right():
    """The timestamp is in the signed material precisely so a replay fails."""
    stale = int(utcnow().timestamp()) - 4000
    header = webhooks.sign("secret", stale, "payload")
    assert not webhooks.verify("secret", header, "payload")


def test_a_malformed_signature_header_is_refused_rather_than_raising():
    assert not webhooks.verify("secret", "nonsense", "payload")
    assert not webhooks.verify("secret", "t=notanumber,v1=abc", "payload")


# --------------------------------------------------------------------------- #
# Which URLs Herald will call                                                  #
# --------------------------------------------------------------------------- #


def test_a_plain_https_url_is_accepted():
    assert webhooks.validate_url("  https://example.test/hook  ") == (
        "https://example.test/hook"
    )


@pytest.mark.parametrize(
    "url", ["ftp://example.test/hook", "example.test/hook", "javascript:alert(1)"]
)
def test_a_non_http_url_is_refused(url):
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.validate_url(url)


def test_a_url_resolving_inside_the_network_is_refused(monkeypatch):
    """The point of the guard: a webhook must not become an SSRF primitive."""
    monkeypatch.setattr(
        link_check,
        "_unreachable_for_a_reader",
        lambda url: "Resolves to a private or loopback address (metadata).",
    )
    with pytest.raises(webhooks.WebhookUrlError) as exc:
        webhooks.validate_url("http://169.254.169.254/latest/meta-data/")
    assert "will not call" in str(exc.value)


# --------------------------------------------------------------------------- #
# Delivery                                                                     #
# --------------------------------------------------------------------------- #


def test_a_2xx_marks_the_delivery_delivered(db, webhook, endpoint):
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.DELIVERED
    assert delivery.response_status == 200
    assert delivery.delivered_at is not None
    assert delivery.next_attempt_at is None
    assert webhook.consecutive_failures == 0


def test_the_request_carries_a_verifiable_signature(db, webhook, endpoint):
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    sent = endpoint.requests[-1]
    header = sent.headers[webhooks.SIGNATURE_HEADER]
    assert webhooks.verify(
        "shhh-a-secret-value", header, sent.content.decode("utf-8")
    )
    assert sent.headers[webhooks.EVENT_HEADER] == "content.published"
    assert sent.headers[webhooks.DELIVERY_HEADER] == str(delivery.id)


def test_the_body_carries_the_event_and_the_delivery_id(db, webhook, endpoint):
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    body = endpoint.last_body
    assert body["event"] == "content.published"
    assert body["id"] == delivery.id
    assert body["data"] == {"hello": "world"}


def test_a_500_is_retried_with_a_backoff(db, webhook, endpoint):
    endpoint.status = 500
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None
    assert as_aware(delivery.next_attempt_at) > utcnow()
    # Not yet a failure against the endpoint — the delivery is still owed.
    assert webhook.consecutive_failures == 0


def test_a_connection_error_is_retried(db, webhook, endpoint):
    endpoint.error = httpx.ConnectError("refused")
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING
    assert "ConnectError" in delivery.error


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_a_client_error_is_final_because_no_retry_would_fix_it(
    db, webhook, endpoint, code
):
    endpoint.status = code
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.attempts == 1
    assert webhook.consecutive_failures == 1


@pytest.mark.parametrize("code", [408, 429])
def test_later_is_not_the_same_as_never(db, webhook, endpoint, code):
    """A timeout and a rate limit are the two 4xx that mean "come back"."""
    endpoint.status = code
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING


def test_a_redirect_is_refused_rather_than_followed(db, webhook, endpoint):
    endpoint.status = 302
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.FAILED
    assert "does not follow" in delivery.error


def test_the_retry_budget_is_finite(db, webhook, endpoint):
    endpoint.status = 503
    delivery = _delivery(db, webhook)

    for _ in range(settings.webhook_max_attempts):
        delivery.next_attempt_at = utcnow()
        webhooks.deliver(db, delivery)

    assert delivery.attempts == settings.webhook_max_attempts
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.next_attempt_at is None


def test_a_url_that_became_private_is_refused_at_delivery_time(
    db, webhook, endpoint, monkeypatch
):
    """The URL passed validation on create; DNS says otherwise now."""
    monkeypatch.setattr(
        link_check, "_unreachable_for_a_reader", lambda url: "Resolves to 127.0.0.1."
    )
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.FAILED
    assert endpoint.requests == []


def test_an_endpoint_that_keeps_failing_is_deactivated(db, webhook, endpoint):
    endpoint.status = 400  # terminal, so each delivery counts once
    for _ in range(settings.webhook_disable_after_failures):
        webhooks.deliver(db, _delivery(db, webhook))

    assert webhook.consecutive_failures == settings.webhook_disable_after_failures
    assert webhook.is_active is False


def test_a_success_clears_the_failure_run(db, webhook, endpoint):
    endpoint.status = 400
    webhooks.deliver(db, _delivery(db, webhook))
    assert webhook.consecutive_failures == 1

    endpoint.status = 200
    webhooks.deliver(db, _delivery(db, webhook))
    assert webhook.consecutive_failures == 0
    assert webhook.last_error is None


def test_the_backoff_doubles_and_is_capped():
    base = settings.webhook_retry_backoff_seconds
    assert webhooks._backoff(1) == base
    assert webhooks._backoff(2) == base * 2
    assert webhooks._backoff(3) == base * 4
    assert webhooks._backoff(50) == settings.webhook_retry_max_backoff_seconds


# --------------------------------------------------------------------------- #
# Emission                                                                     #
# --------------------------------------------------------------------------- #


def test_emit_reaches_only_subscribed_active_endpoints(db, user, endpoint):
    subscribed = Webhook(
        user_id=user.id,
        url=_URL,
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("s1"),
    )
    other_event = Webhook(
        user_id=user.id,
        url=_URL,
        events=[WebhookEvent.PUBLICATION_FAILED.value],
        encrypted_secret=webhooks.store_secret("s2"),
    )
    inactive = Webhook(
        user_id=user.id,
        url=_URL,
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        is_active=False,
        encrypted_secret=webhooks.store_secret("s3"),
    )
    db.add_all([subscribed, other_event, inactive])
    db.commit()

    deliveries = webhooks.emit(
        db,
        user_id=user.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        data={"content": {"id": 1}},
    )

    assert [d.webhook_id for d in deliveries] == [subscribed.id]


def test_emit_does_not_cross_between_users(db, user, endpoint):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="someone@example.com",
        full_name="Someone",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    db.add(
        Webhook(
            user_id=stranger.id,
            url=_URL,
            events=[WebhookEvent.CONTENT_PUBLISHED.value],
            encrypted_secret=webhooks.store_secret("s"),
        )
    )
    db.commit()

    assert (
        webhooks.emit(
            db, user_id=user.id, event=WebhookEvent.CONTENT_PUBLISHED, data={}
        )
        == []
    )


def test_emit_never_raises_when_delivery_blows_up(db, webhook, monkeypatch):
    """A webhook is a side channel; it must not be able to fail the publish."""

    def _boom() -> httpx.Client:
        raise RuntimeError("the transport is on fire")

    monkeypatch.setattr(webhooks, "_http_client", _boom)
    deliveries = webhooks.emit(
        db, user_id=webhook.user_id, event=WebhookEvent.CONTENT_PUBLISHED, data={}
    )
    assert len(deliveries) == 1


def test_a_retry_replays_the_body_that_was_frozen_at_emit_time(
    db, webhook, endpoint
):
    endpoint.status = 503
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)
    first = endpoint.last_body

    # The world moves on; the delivery does not.
    delivery.next_attempt_at = utcnow()
    endpoint.status = 200
    webhooks.deliver(db, delivery)

    assert endpoint.last_body == first


def test_due_deliveries_skips_rows_still_in_backoff(db, webhook):
    from datetime import timedelta

    ready = _delivery(db, webhook)
    waiting = _delivery(db, webhook)
    waiting.next_attempt_at = utcnow() + timedelta(hours=1)
    settled = _delivery(db, webhook)
    settled.status = DeliveryStatus.DELIVERED
    settled.next_attempt_at = None
    db.commit()

    assert [d.id for d in webhooks.due_deliveries(db)] == [ready.id]


# --------------------------------------------------------------------------- #
# Wired into publishing                                                        #
# --------------------------------------------------------------------------- #


def _content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        title="Herald ships webhooks",
        slug="herald-ships-webhooks",
        body_markdown="It calls you now.",
        excerpt="It calls you now.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_first_publish_fires_content_published(db, project, webhook, endpoint):
    from app.services import publishing_service

    content = _content(db, project)
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_url="https://dev.to/x/herald-ships-webhooks",
    )
    db.add(publication)
    db.commit()

    publishing_service._notify_published(db, content, publication)

    body = endpoint.last_body
    assert body["event"] == "content.published"
    assert body["data"]["content"]["title"] == "Herald ships webhooks"
    assert body["data"]["content"]["project"]["name"] == project.name
    assert body["data"]["publication"]["platform"] == "devto"


def test_a_terminal_failure_fires_publication_failed(db, project, user, endpoint):
    from app.services import publishing_service

    db.add(
        Webhook(
            user_id=user.id,
            url=_URL,
            events=[WebhookEvent.PUBLICATION_FAILED.value],
            encrypted_secret=webhooks.store_secret("s"),
        )
    )
    content = _content(db, project)
    publication = Publication(
        content_id=content.id,
        platform=Platform.MASTODON,
        status=PublicationStatus.FAILED,
        error="Instance said no.",
        attempts=3,
    )
    db.add(publication)
    db.commit()

    publishing_service._notify_failed(db, publication)

    body = endpoint.last_body
    assert body["event"] == "publication.failed"
    assert body["data"]["publication"]["error"] == "Instance said no."
    assert body["data"]["publication"]["attempts"] == 3


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #


def test_the_secret_is_returned_once_and_never_again(client, auth, endpoint):
    created = client.post(
        "/api/v1/webhooks",
        json={"url": _URL, "events": ["content.published"], "description": "Slack"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    assert secret

    listed = client.get("/api/v1/webhooks", headers=auth)
    assert listed.status_code == 200
    assert "secret" not in listed.json()[0]

    detail = client.get(
        f"/api/v1/webhooks/{created.json()['id']}/deliveries", headers=auth
    )
    assert "secret" not in detail.text


def test_rotating_the_secret_invalidates_the_old_one(client, auth, db, endpoint):
    created = client.post(
        "/api/v1/webhooks",
        json={"url": _URL, "events": ["content.published"]},
        headers=auth,
    ).json()

    rotated = client.post(
        f"/api/v1/webhooks/{created['id']}/rotate-secret", headers=auth
    )
    assert rotated.status_code == 200
    assert rotated.json()["secret"] != created["secret"]

    client.post(f"/api/v1/webhooks/{created['id']}/ping", headers=auth)
    sent = endpoint.requests[-1]
    header = sent.headers[webhooks.SIGNATURE_HEADER]
    assert not webhooks.verify(created["secret"], header, sent.content.decode())
    assert webhooks.verify(rotated.json()["secret"], header, sent.content.decode())


def test_creating_a_webhook_for_a_private_address_is_refused(
    client, auth, monkeypatch
):
    monkeypatch.setattr(
        link_check, "_unreachable_for_a_reader", lambda url: "Loopback."
    )
    resp = client.post(
        "/api/v1/webhooks",
        json={"url": "http://127.0.0.1:8000/hook", "events": ["content.published"]},
        headers=auth,
    )
    assert resp.status_code == 422
    assert "will not call" in resp.json()["detail"]


def test_subscribing_to_ping_is_refused(client, auth):
    resp = client.post(
        "/api/v1/webhooks",
        json={"url": _URL, "events": ["webhook.ping"]},
        headers=auth,
    )
    assert resp.status_code == 422


def test_a_webhook_needs_at_least_one_event(client, auth):
    resp = client.post(
        "/api/v1/webhooks", json={"url": _URL, "events": []}, headers=auth
    )
    assert resp.status_code == 422


def test_ping_delivers_immediately_regardless_of_subscription(
    client, auth, db, endpoint
):
    created = client.post(
        "/api/v1/webhooks",
        json={"url": _URL, "events": ["publication.failed"]},
        headers=auth,
    ).json()

    resp = client.post(f"/api/v1/webhooks/{created['id']}/ping", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    assert endpoint.last_body["event"] == "webhook.ping"


def test_reactivating_an_endpoint_clears_its_failure_run(client, auth, db, webhook):
    webhook.consecutive_failures = 7
    webhook.is_active = False
    db.commit()

    resp = client.patch(
        f"/api/v1/webhooks/{webhook.id}", json={"is_active": True}, headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["consecutive_failures"] == 0


def test_deleting_a_webhook_takes_its_deliveries_with_it(client, auth, db, webhook):
    _delivery(db, webhook)
    assert client.delete(f"/api/v1/webhooks/{webhook.id}", headers=auth).status_code == 204
    assert db.query(WebhookDelivery).count() == 0


def test_another_users_webhook_is_a_404(client, auth, db):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    hook = Webhook(
        user_id=stranger.id,
        url=_URL,
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhooks.store_secret("s"),
    )
    db.add(hook)
    db.commit()

    assert client.get(f"/api/v1/webhooks/{hook.id}/deliveries", headers=auth).status_code == 404
    assert client.delete(f"/api/v1/webhooks/{hook.id}", headers=auth).status_code == 404
    assert client.post(f"/api/v1/webhooks/{hook.id}/ping", headers=auth).status_code == 404


def test_redelivering_replays_the_original_body(client, auth, db, webhook, endpoint):
    endpoint.status = 400
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)
    assert delivery.status == DeliveryStatus.FAILED

    endpoint.status = 200
    resp = client.post(
        f"/api/v1/webhooks/{webhook.id}/deliveries/{delivery.id}/redeliver",
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    assert resp.json()["attempts"] == 1
    assert endpoint.last_body["data"] == {"hello": "world"}


def test_deliveries_can_be_filtered_by_status(client, auth, db, webhook, endpoint):
    webhooks.deliver(db, _delivery(db, webhook))
    endpoint.status = 400
    webhooks.deliver(db, _delivery(db, webhook))

    resp = client.get(
        f"/api/v1/webhooks/{webhook.id}/deliveries?status=failed", headers=auth
    )
    assert resp.status_code == 200
    assert [d["status"] for d in resp.json()] == ["failed"]
    assert resp.headers["X-Total-Count"] == "1"


def test_the_event_catalogue_lists_what_can_be_subscribed_to(client, auth):
    resp = client.get("/api/v1/webhooks/events", headers=auth)
    assert resp.status_code == 200
    events = {row["event"] for row in resp.json()}
    assert events == {"content.published", "publication.failed", "review.pending"}
    assert all(row["description"] for row in resp.json())


def test_webhooks_require_authentication(client):
    assert client.get("/api/v1/webhooks").status_code == 401
    assert client.post("/api/v1/webhooks", json={}).status_code == 401
