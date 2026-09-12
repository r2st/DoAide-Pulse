"""Outbound webhooks: subscription, signing, delivery, retry and refusal.

The network is stubbed with ``httpx.MockTransport`` and DNS is stubbed out, so
what is under test is Herald's own decision-making — which endpoint hears about
what, what the body says, what counts as a failure worth retrying, and which
URLs the server refuses to open at all.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from app.config import settings
from app.logging_config import request_id_var
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


def test_the_request_carries_the_id_of_whatever_caused_it(db, webhook, endpoint):
    """The receiver's half of the trace.

    A delivery is dispatched by an API request or re-armed by a sweep, and both
    already stamp every line Herald writes about it. Sending the same id lets a
    receiver debugging "you posted me something wrong" quote something that
    appears in Herald's own journal, instead of a timestamp and a description.
    """
    token = request_id_var.set("abc123def456")
    try:
        delivery = _delivery(db, webhook)
        webhooks.deliver(db, delivery)
    finally:
        request_id_var.reset(token)

    assert endpoint.requests[-1].headers[webhooks.CORRELATION_HEADER] == "abc123def456"


def test_the_correlation_id_is_outside_the_signature(db, webhook, endpoint):
    """``sign`` covers the timestamp and the body, and widening it to a header
    would break every receiver already verifying deliveries — the signature
    scheme is Stripe's precisely so that existing libraries can check it."""
    token = request_id_var.set("abc123def456")
    try:
        delivery = _delivery(db, webhook)
        webhooks.deliver(db, delivery)
    finally:
        request_id_var.reset(token)

    sent = endpoint.requests[-1]
    assert webhooks.verify(
        "shhh-a-secret-value",
        sent.headers[webhooks.SIGNATURE_HEADER],
        sent.content.decode("utf-8"),
    )


def test_a_delivery_with_no_request_behind_it_still_sends_the_header(
    db, webhook, endpoint
):
    """``-`` rather than an absent header: a receiver that reads it
    unconditionally should not have to handle two shapes, and the marker says
    "nothing dispatched this" — which is true of a sweep-driven retry."""
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)

    assert endpoint.requests[-1].headers[webhooks.CORRELATION_HEADER] == "-"


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


def test_a_switched_off_endpoint_is_not_posted_to(db, webhook, endpoint):
    """A delivery in backoff outlives the decision to disable its endpoint.

    ``emit`` never creates a delivery for an inactive endpoint, but the sweep
    retries the ones that already exist — so switching an endpoint off used to
    mean "no *new* events", while the four in backoff kept arriving.
    """
    delivery = _delivery(db, webhook)
    webhook.is_active = False
    db.commit()

    webhooks.deliver(db, delivery)

    assert endpoint.requests == []
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.next_attempt_at is None
    assert "switched off" in (delivery.error or "")


def test_a_skipped_delivery_does_not_count_against_the_endpoint(
    db, webhook, endpoint
):
    """Choosing not to send is not evidence that the endpoint is broken.

    Routed through ``_record_failure`` a skip would move
    ``consecutive_failures``, and an endpoint the breaker had just tripped
    would be tripped again by the deliveries the trip stranded.
    """
    webhook.is_active = False
    webhook.consecutive_failures = 2
    db.commit()

    webhooks.deliver(db, _delivery(db, webhook))

    assert webhook.consecutive_failures == 2
    assert webhook.last_error is None


def test_the_breaker_strands_nothing_that_still_posts(db, webhook, endpoint):
    """The deliveries that tripped the breaker stop with it.

    Each terminal failure counts once, so after ``webhook_disable_after_failures``
    of them the endpoint is off. A fifth delivery that was already queued must
    then go nowhere, rather than being the one POST the disable did not stop.
    """
    endpoint.status = 400
    stranded = _delivery(db, webhook)
    stranded.next_attempt_at = utcnow() + timedelta(hours=1)
    db.commit()
    for _ in range(settings.webhook_disable_after_failures):
        webhooks.deliver(db, _delivery(db, webhook))
    assert webhook.is_active is False
    sent_before = len(endpoint.requests)

    stranded.next_attempt_at = utcnow()
    db.commit()
    webhooks.deliver(db, stranded)

    assert len(endpoint.requests) == sent_before
    assert stranded.status == DeliveryStatus.FAILED


def test_a_deactivated_account_sends_no_webhooks(db, user, webhook, endpoint):
    """Closing the account closes this door too — like every other sweep."""
    delivery = _delivery(db, webhook)
    user.is_active = False
    db.commit()

    webhooks.deliver(db, delivery)

    assert endpoint.requests == []
    assert delivery.status == DeliveryStatus.FAILED
    assert "deactivated" in (delivery.error or "")


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
    assert events == {
        "content.published",
        "publication.failed",
        "review.pending",
        "content.engagement_threshold",
    }
    assert all(row["description"] for row in resp.json())


def test_webhooks_require_authentication(client):
    assert client.get("/api/v1/webhooks").status_code == 401
    assert client.post("/api/v1/webhooks", json={}).status_code == 401


# --------------------------------------------------------------------------- #
# Claiming: one attempt has one sender                                         #
# --------------------------------------------------------------------------- #
#
# The retry story above is all single-threaded. In production two things race
# for the same pending row by default: the ``deliver_one`` queued at emit time
# and the beat sweep, which every delivery passes through, and two overlapping
# sweeps, which the shipped configuration makes ordinary rather than rare (the
# beat runs every 60s; one sweep is up to 200 deliveries at a 10s timeout).
# Neither duplicate is distinguishable at the receiver from the retry the
# delivery id exists to let it collapse.


def test_a_delivery_already_claimed_is_not_sent_again(db, webhook, endpoint):
    """The race, reduced to its two steps: somebody else has it."""
    delivery = _delivery(db, webhook)
    assert webhooks.claim(db, delivery) is True

    webhooks.deliver(db, delivery)

    assert endpoint.requests == []
    assert delivery.attempts == 1


def test_two_sweeps_over_the_same_due_row_send_it_once(db, webhook, endpoint):
    """The production race, at the level it actually happens.

    Both sweeps select the row while it is pending and due — that part is
    unchanged and unavoidable, since selecting is not claiming. What must not
    happen is two POSTs.
    """
    delivery = _delivery(db, webhook)

    first = webhooks.due_deliveries(db)
    second = webhooks.due_deliveries(db)
    assert [d.id for d in first] == [d.id for d in second] == [delivery.id]

    for row in first + second:
        webhooks.deliver(db, row)

    assert len(endpoint.requests) == 1
    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.DELIVERED
    # The loser did not spend one either.
    assert delivery.attempts == 1


def test_a_losing_claim_does_not_spend_the_retry_budget(db, webhook, endpoint):
    """Otherwise concurrency alone would exhaust a delivery.

    Five racing workers against a five-attempt budget would fail the delivery
    outright without a single endpoint refusal, which is the failure mode that
    makes an unclaimed retry loop worse than no retry loop.
    """
    delivery = _delivery(db, webhook)
    assert webhooks.claim(db, delivery) is True

    for _ in range(settings.webhook_max_attempts + 3):
        assert webhooks.claim(db, delivery) is False

    assert delivery.attempts == 1
    assert delivery.status == DeliveryStatus.PENDING


@pytest.mark.parametrize(
    "settled", [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED]
)
def test_a_settled_delivery_is_never_sent_again(db, webhook, endpoint, settled):
    """``deliver`` is now safe to call on a row that is finished with.

    A redelivery has to go through ``requeue``, which is a person saying the
    endpoint is fixed. Reaching the POST from a terminal row would mean a
    duplicate nobody asked for and no retry accounted for.
    """
    delivery = _delivery(db, webhook)
    delivery.status = settled
    delivery.next_attempt_at = None
    db.commit()

    webhooks.deliver(db, delivery)

    assert endpoint.requests == []
    assert delivery.attempts == 0
    assert delivery.status == settled


def test_a_backoff_that_has_not_elapsed_is_not_claimable(db, webhook, endpoint):
    """The claim re-checks due-ness, not just pending-ness.

    Without the second half of the condition the claim would degrade into "is
    it pending", and a sweep would ignore every backoff it just set.
    """
    delivery = _delivery(db, webhook)
    delivery.next_attempt_at = utcnow() + timedelta(minutes=5)
    db.commit()

    assert webhooks.claim(db, delivery) is False
    assert delivery.attempts == 0


def test_requeue_makes_a_failed_delivery_claimable_again(db, webhook, endpoint):
    """The redeliver button, end to end, against the guard above."""
    endpoint.status = 400
    delivery = _delivery(db, webhook)
    webhooks.deliver(db, delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert len(endpoint.requests) == 1

    endpoint.status = 200
    webhooks.requeue(db, delivery)
    webhooks.deliver(db, delivery)

    assert len(endpoint.requests) == 2
    assert delivery.status == DeliveryStatus.DELIVERED


def test_a_claim_holds_for_a_lease_and_then_lets_go(db, webhook, endpoint):
    """A worker killed mid-attempt must not park the delivery forever.

    Nothing releases a claim on a process that died, so the lease expiring is
    the only thing that can — which makes it the crash-recovery window, and the
    reason it is a timestamp rather than a flag.
    """
    delivery = _delivery(db, webhook)
    assert webhooks.claim(db, delivery) is True

    # Still owned: the sweep does not see it.
    assert webhooks.due_deliveries(db) == []
    assert as_aware(delivery.next_attempt_at) > utcnow()

    # The worker never came back. The lease runs out.
    delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()

    assert [d.id for d in webhooks.due_deliveries(db)] == [delivery.id]
    assert webhooks.claim(db, delivery) is True
    assert delivery.attempts == 2


def test_the_lease_outlasts_the_request_it_covers(db, webhook, endpoint):
    """A lease shorter than the timeout expires mid-request.

    That would hand the row to a second worker while the first is still waiting
    on the socket — reintroducing the duplicate through the mechanism meant to
    prevent it.
    """
    assert settings.webhook_claim_lease_seconds > settings.webhook_timeout_seconds


def test_the_receiver_sees_one_delivery_id_across_every_attempt(
    db, webhook, endpoint
):
    """The header a receiver dedupes on has to be stable to be worth anything.

    A retry that arrived under a fresh id would be a new event as far as the
    far end could tell, and the claim above would be the only thing standing
    between a flaky endpoint and a duplicate announcement.
    """
    endpoint.status = 503
    delivery = _delivery(db, webhook)

    for _ in range(3):
        delivery.next_attempt_at = utcnow()
        db.commit()
        webhooks.deliver(db, delivery)

    assert len(endpoint.requests) == 3
    seen = {r.headers[webhooks.DELIVERY_HEADER] for r in endpoint.requests}
    assert seen == {str(delivery.id)}
    assert {json.loads(r.content)["id"] for r in endpoint.requests} == {delivery.id}


# --------------------------------------------------------------------------- #
# Paging the listing                                                           #
# --------------------------------------------------------------------------- #


def _endpoints(db, user_id: int, count: int) -> None:
    for index in range(count):
        db.add(
            Webhook(
                user_id=user_id,
                url=f"https://hooks.example.com/{index}",
                description=f"Endpoint {index}",
                events=["content.published"],
            )
        )
    db.commit()


def test_the_listing_pages_and_counts(client, auth, db, user):
    """``MAX_WEBHOOKS_PER_USER`` is checked in ``create``, not on this read.

    Written straight to the table for that reason: the point of paging the
    listing is that a row can exist without ``create`` having agreed to it —
    seeded, or left behind by a cap that was lowered.
    """
    _endpoints(db, user.id, 5)

    resp = client.get("/api/v1/webhooks?limit=2&offset=1", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["description"] for row in resp.json()] == ["Endpoint 1", "Endpoint 2"]
    assert resp.headers["X-Total-Count"] == "5"


def test_the_listing_still_answers_with_everything_by_default(client, auth, db, user):
    _endpoints(db, user.id, 3)

    resp = client.get("/api/v1/webhooks", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3
    assert resp.headers["X-Total-Count"] == "3"


def test_the_count_is_this_account_s_alone(client, auth, db, user):
    """The header is a count of the same query the page came from, not of the table."""
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="counted@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()

    _endpoints(db, user.id, 2)
    _endpoints(db, stranger.id, 4)

    resp = client.get("/api/v1/webhooks", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2
    assert resp.headers["X-Total-Count"] == "2"


@pytest.mark.parametrize("limit", [-1, 0, 21])
def test_a_limit_outside_the_range_is_refused(client, auth, limit):
    resp = client.get(f"/api/v1/webhooks?limit={limit}", headers=auth)
    assert resp.status_code == 422, resp.text
