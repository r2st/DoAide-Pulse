"""What happens to a delivery when the endpoint is broken rather than unhappy.

``test_webhooks.py`` covers the endpoint that *answers*: a 500, a 4xx, a
redirect, a rate limit. Each of those is a status code, and the decision follows
from it. This file covers the failures where there is no status code at all,
because the exchange never got far enough to have one — the certificate does not
verify, the handshake stalls, the endpoint accepts the connection and then says
nothing for longer than Pulse is prepared to wait.

They matter for a different reason than a 500 does. Every one of them is
``httpx.HTTPError``, and ``deliver`` has a single arm for that whole family:

    except httpx.HTTPError as exc:
        _record_failure(db, delivery, f"{type(exc).__name__}: {exc}")

One line, no status, non-terminal — so the classification is inherited rather
than decided, and the question worth pinning is whether inheriting it is right
for each case. It is, and not obviously:

* A **bad certificate** is a configuration mistake that will not fix itself, so
  there is an argument for failing it terminally. Against that: an expired cert
  is the single most common way a working endpoint breaks, it is fixed within
  hours, and the retry budget is finite anyway. Retrying is the forgiving
  reading and the right one — but it must be *bounded*, which is the half that
  would break silently.
* A **slow endpoint** must not be retried into a stampede, and must not hold a
  worker: the timeout is what makes a delivery's cost predictable, and it is
  set on the client rather than per call.

Nothing here is a new mechanism. It is the arm that no test had walked with
anything other than a ``ConnectError``, which is the one member of the family
that reaches it by the shortest path.
"""
from __future__ import annotations

import json
import ssl

import httpx
import pytest

from app.config import settings
from app.models.mixins import as_aware, utcnow
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services import link_check, webhooks

_URL = "https://hooks.example.test/pulse"

#: What httpx raises when the certificate does not verify. Built the way the
#: real one is — an ``ssl`` error wrapped in a transport error — because the
#: message Pulse records is the wrapped one's, and a hand-written
#: ``ConnectError("bad cert")`` would not tell us what a user actually sees.
def _tls_failure() -> httpx.ConnectError:
    underlying = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "certificate has expired (_ssl.c:1006)",
    )
    error = httpx.ConnectError(str(underlying))
    error.__cause__ = underlying
    return error


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Every hostname is public — the URL guard is not what is under test."""
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


class _Endpoint:
    """A receiver that fails the way it is told to, and counts the attempts."""

    def __init__(self, error: Exception):
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise self.error


@pytest.fixture
def failing(monkeypatch):
    """Install a receiver that raises *error* for every delivery."""

    def install(error: Exception) -> _Endpoint:
        receiver = _Endpoint(error)
        monkeypatch.setattr(
            webhooks,
            "_http_client",
            lambda: httpx.Client(
                transport=httpx.MockTransport(receiver), follow_redirects=False
            ),
        )
        return receiver

    return install


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


def _delivery(db, webhook: Webhook) -> WebhookDelivery:
    event = WebhookEvent.CONTENT_PUBLISHED
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


#: The family, each member reaching ``deliver``'s ``httpx.HTTPError`` arm by a
#: different route. ``ConnectError`` is the one the existing suite already
#: walks; the rest are here because they are the ones a real endpoint produces.
_TRANSPORT_FAILURES = [
    ("expired-certificate", _tls_failure()),
    ("handshake-timeout", httpx.ConnectTimeout("timed out during handshake")),
    ("slow-response", httpx.ReadTimeout("timed out waiting for a response")),
    ("dropped-mid-send", httpx.WriteError("broken pipe")),
    ("disconnected", httpx.RemoteProtocolError("server disconnected")),
    ("pool-exhausted", httpx.PoolTimeout("no connection available")),
]
_IDS = [name for name, _ in _TRANSPORT_FAILURES]
_FAILURES = [failure for _, failure in _TRANSPORT_FAILURES]


# --------------------------------------------------------------------------- #
# Each of them is a retry, not a verdict                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_a_broken_transport_is_retried_rather_than_given_up_on(
    db, webhook, failing, failure
):
    """None of these says the subscription is wrong, so none is terminal.

    The delivery stays ``PENDING`` with a future ``next_attempt_at``, which is
    what the sweep looks for. Marked ``FAILED`` here instead, an endpoint whose
    certificate expired overnight would lose every event of the outage rather
    than catching up when it was renewed.
    """
    failing(failure)
    delivery = _delivery(db, webhook)

    webhooks.deliver(db, delivery)

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None
    assert as_aware(delivery.next_attempt_at) > utcnow()


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_the_failure_says_which_kind_it_was(db, webhook, failing, failure):
    """``last_error`` is what the user has to debug from.

    "Delivery failed" would be true of all six and useful for none. Each
    failure type produces a distinct friendly message so an expired certificate
    and a slow endpoint are not the same line in the UI.
    """
    from app.services.errors import friendly_network_error

    failing(failure)
    delivery = _delivery(db, webhook)

    webhooks.deliver(db, delivery)

    assert delivery.error == friendly_network_error(failure)


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_no_transport_failure_retries_for_ever(db, webhook, failing, failure):
    """The forgiving reading of a broken endpoint still has to end.

    This is the half that would rot quietly: "retry it" is right, and "retry it
    without a ceiling" is a queue that grows for as long as a certificate stays
    expired. The budget is the same finite one a 503 gets.
    """
    failing(failure)
    delivery = _delivery(db, webhook)

    for _ in range(settings.webhook_max_attempts):
        delivery.next_attempt_at = utcnow()
        webhooks.deliver(db, delivery)

    assert delivery.attempts == settings.webhook_max_attempts
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.next_attempt_at is None


def test_an_endpoint_that_never_answers_stops_being_delivered_to(
    db, webhook, failing
):
    """A run of failures deactivates the endpoint, however they failed.

    The deactivation counter is what stops a dead endpoint costing a delivery
    attempt per event for ever. It is driven by *terminal* failures, so it has
    to be reached by this route too — an endpoint whose certificate expired and
    was never renewed must eventually be switched off, exactly like one
    answering 404.
    """
    failing(_tls_failure())

    for _ in range(settings.webhook_disable_after_failures):
        delivery = _delivery(db, webhook)
        for _ in range(settings.webhook_max_attempts):
            delivery.next_attempt_at = utcnow()
            webhooks.deliver(db, delivery)

    db.refresh(webhook)
    assert not webhook.is_active


# --------------------------------------------------------------------------- #
# What the failure is allowed to cost                                          #
# --------------------------------------------------------------------------- #


def test_a_slow_endpoint_cannot_hold_a_worker_indefinitely(monkeypatch):
    """The timeout is on the client, so every delivery inherits it.

    Set per-request instead, a path that forgot it would hang a worker on one
    endpoint. ``test_every_outbound_call_has_a_timeout.py`` makes the general
    version of this claim; this pins the specific number to the setting, because
    the delivery timeout is also what the claim lease is sized against — see
    ``claim()``, whose lease must outlast the request it covers.
    """
    client = webhooks._http_client()
    try:
        assert client.timeout.read == settings.webhook_timeout_seconds
        assert client.timeout.connect == settings.webhook_timeout_seconds
    finally:
        client.close()


def test_the_lease_still_outlasts_the_slowest_possible_delivery():
    """A claim that expires mid-request is a delivery sent twice.

    Stated as an inequality between two settings rather than as fixed numbers,
    so raising the timeout without raising the lease fails here rather than in
    production as a duplicate.
    """
    assert settings.webhook_claim_lease_seconds > settings.webhook_timeout_seconds


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_the_endpoints_own_words_cannot_be_unbounded(db, webhook, failing, failure):
    """An exception message is a string from outside, like a response body.

    ``test_webhook_sweep.py`` bounds what a *responding* endpoint says. The
    exception path builds its message the same way — by interpolating something
    Pulse did not write — and writes it to the same ``Text`` column.
    """
    from app.services.errors import MAX_ERROR_CHARS

    failing(type(failure)("x" * (MAX_ERROR_CHARS * 3)))
    delivery = _delivery(db, webhook)

    webhooks.deliver(db, delivery)

    assert len(delivery.error) <= MAX_ERROR_CHARS


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_a_broken_transport_never_leaks_the_signing_secret(
    db, webhook, failing, failure
):
    """The secret is read to sign the body and must not survive into the row.

    ``last_error`` is rendered on the webhooks page; the secret is shown once at
    creation and never again (``test_webhooks.py``). A failure message that
    quoted the outgoing headers would undo that.
    """
    failing(failure)
    delivery = _delivery(db, webhook)

    webhooks.deliver(db, delivery)

    assert "shhh-a-secret-value" not in delivery.error


# --------------------------------------------------------------------------- #
# What the receiver saw                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_a_retry_after_a_broken_transport_replays_the_frozen_body(
    db, webhook, failing, failure
):
    """The payload was frozen at emit time and must not be rebuilt.

    A transport failure is the case where a retry is most likely, so it is worth
    saying here as well as for a 500: the second attempt carries the same bytes
    and the same delivery id, because a receiver deduplicating on that id has to
    be able to.
    """
    receiver = failing(failure)
    delivery = _delivery(db, webhook)

    webhooks.deliver(db, delivery)
    delivery.next_attempt_at = utcnow()
    webhooks.deliver(db, delivery)

    assert len(receiver.requests) == 2
    first, second = receiver.requests
    assert first.content == second.content
    assert json.loads(first.content)["id"] == delivery.id
    assert (
        first.headers[webhooks.DELIVERY_HEADER]
        == second.headers[webhooks.DELIVERY_HEADER]
    )
