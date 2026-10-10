"""Emitting, signing and delivering outbound webhooks.

Three things here are load-bearing, and each exists because the naive version is
wrong in a way that only shows up in production.

**The URL is not trusted.** It is typed by a user into a box, and Pulse's
server is what opens it. ``http://169.254.169.254/`` is a valid URL, and a
webhook pointed at it is a request for the cloud metadata endpoint delivered by
a process that can reach it. So the host is resolved and checked against
loopback, private and link-local space — at creation *and* again at delivery,
because DNS can change between the two — and redirects are not followed at all.
A 3xx is recorded as a failure with the reason, which is honest: Pulse cannot
tell a legitimate redirect from one that has just moved the target inside the
network.

**The signature is over a timestamp too.** ``X-Pulse-Signature: t=…,v1=…`` is
HMAC-SHA256 over ``{timestamp}.{body}``, which lets a receiver reject a replay
of a body it has already seen. Signing the body alone would authenticate the
payload without saying anything about when it was sent.

**A retry is not a new event.** The body is frozen when the delivery row is
created, so an endpoint that comes back an hour later is told what happened,
not what is true now. That is the whole reason the payload lives on the
delivery rather than being rebuilt from the content row at send time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import request_id_var
from app.models.mixins import utcnow
from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.services import link_check, signals
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.services.errors import clip_error, friendly_network_error, sanitize_unexpected_error

logger = logging.getLogger(__name__)

#: ``t=<unix seconds>,v1=<hex hmac>``. The scheme is Stripe's, and deliberately
#: so — it is the one every webhook-receiving library already knows how to
#: verify, and inventing a private format would only mean nobody verifies at all.
SIGNATURE_HEADER = "X-Pulse-Signature"
EVENT_HEADER = "X-Pulse-Event"
DELIVERY_HEADER = "X-Pulse-Delivery"

#: The id of whatever caused this delivery — the API request, or the sweep that
#: re-armed it — so a receiver debugging "you sent me something wrong" can quote
#: an id that appears in Pulse's own journal. Outside the signature on purpose:
#: :func:`sign` covers the timestamp and the body, and widening it to a header
#: would break every receiver already verifying deliveries.
#:
#: Distinct from ``X-Request-ID``, which the API *returns* on its own responses.
#: This is the same value travelling in the other direction, and naming it apart
#: keeps a receiver's own request id from being overwritten by ours.
CORRELATION_HEADER = "X-Pulse-Request-ID"

#: The prefix a signature's version field carries.
SIGNATURE_VERSION = "v1"

#: What a ``v1=`` value is allowed to look like. See :func:`verify`.
_HEX = re.compile(r"[0-9a-fA-F]{64}")


class WebhookUrlError(ValueError):
    """The endpoint URL is one Pulse will not call."""


def generate_secret() -> str:
    """A fresh signing secret. 256 bits, URL-safe, shown once."""
    return secrets.token_urlsafe(32)


def store_secret(secret: str) -> str:
    """Encrypt a signing secret for the ``encrypted_secret`` column."""
    return encrypt_credentials({"secret": secret})


def read_secret(webhook: Webhook) -> str:
    """The signing secret, or ``""`` if it cannot be read.

    A key rotation makes stored secrets unreadable. That is not a reason to
    crash a delivery sweep — the delivery fails with a message that says what
    happened, and the user rotates the webhook secret.
    """
    try:
        return str(decrypt_credentials(webhook.encrypted_secret).get("secret") or "")
    except CredentialEncryptionError as exc:
        logger.warning("webhook %s secret unreadable: %s", webhook.id, exc)
        return ""


def validate_url(url: str) -> str:
    """Return *url* stripped, or raise :class:`WebhookUrlError`.

    Called on create and on update. Delivery re-checks rather than trusting this
    result: the two happen at different times, and a hostname that resolved to
    the public internet on Tuesday can resolve to ``127.0.0.1`` on Wednesday.
    """
    candidate = (url or "").strip()
    if not candidate.lower().startswith(("http://", "https://")):
        raise WebhookUrlError("A webhook URL must start with http:// or https://")
    if len(candidate) > 700:
        raise WebhookUrlError("That URL is too long (700 characters maximum)")

    unreachable = link_check.unreachable_reason(candidate)
    if unreachable:
        raise WebhookUrlError(f"Pulse will not call that URL — {unreachable}")
    return candidate


def sign(secret: str, timestamp: int, body: str) -> str:
    """The value of :data:`SIGNATURE_HEADER` for one request."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},{SIGNATURE_VERSION}={digest}"


def verify(secret: str, header: str, body: str, *, tolerance_seconds: int = 300) -> bool:
    """Whether *header* is a valid, recent signature for *body*.

    Used two ways. Pulse never receives its own webhooks, so for the *outbound*
    scheme this exists to make the contract executable — the test suite verifies
    what a receiver would, and anyone writing a handler can read one function
    instead of inferring the scheme from a docstring. But it is also what
    ``POST /triggers/inbound/{token}`` checks a signed inbound request with, and
    that is an unauthenticated endpoint reached by anybody holding the URL.

    So every input is treated as hostile, and the function's contract is that it
    returns ``False`` rather than raising for *any* header a caller can send. The
    hex check is what makes that true: ``hmac.compare_digest`` raises TypeError
    on a string with a non-ASCII character in it, so a signature of ``v1=café``
    would otherwise take a 401 and turn it into a 500 — an unauthenticated
    request choosing which error the server returns.
    """
    parts = dict(
        piece.split("=", 1) for piece in (header or "").split(",") if "=" in piece
    )
    signature = parts.get(SIGNATURE_VERSION, "").strip()
    # A real signature is SHA-256 hex and nothing else. Anything that is not
    # cannot match, and comparing it is what breaks.
    if not signature or not _HEX.fullmatch(signature):
        return False
    try:
        timestamp = int(parts.get("t", "").strip())
    except ValueError:
        return False
    if abs(int(utcnow().timestamp()) - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body).split(f"{SIGNATURE_VERSION}=", 1)[1]
    return hmac.compare_digest(expected, signature)


#: What GitHub signs its deliveries with, and the two headers that come with it.
#: A different scheme from Pulse's own, and it has to be: GitHub decides the
#: format of the requests GitHub sends, and a push event is the single most
#: likely thing anybody points ``POST /triggers/inbound/{token}`` at.
#:
#: Without this, the only way to use a GitHub webhook was to leave
#: ``require_signature`` off — an unauthenticated endpoint whose whole
#: credential is a URL that has to be pasted into a third-party settings page,
#: which is exactly where URLs leak from.
GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
#: GitHub's per-delivery UUID. Stable across its own redeliveries of the *same*
#: event and different for a new one, which makes it the ideal replay nonce —
#: see :func:`replay_nonce`.
GITHUB_DELIVERY_HEADER = "X-GitHub-Delivery"
_GITHUB_PREFIX = "sha256="


def verify_github(secret: str, header: str, raw_body: bytes) -> bool:
    """Whether *header* is GitHub's ``sha256=`` HMAC over *raw_body*.

    Three differences from :func:`verify`, all of them GitHub's:

    * **No timestamp**, so nothing here bounds how old a delivery may be. That
      is what makes :func:`replay_nonce` load-bearing rather than belt-and-braces
      on this path — a captured GitHub delivery is replayable forever on the
      signature alone.
    * **Over the raw bytes**, not a decoded string. GitHub HMACs the body
      exactly as sent, and a body round-tripped through
      ``decode("utf-8", "replace")`` is a *different* byte string the moment it
      contains a sequence that does not decode — the replacement character is
      three bytes where the original was one. Verifying the decoded form would
      fail every delivery carrying an emoji in a commit message on some
      encodings, and, worse, would be verifying something other than what was
      received.
    * **No version prefix to parse.** The whole value is ``sha256=<hex>``.

    Hostile-input contract identical to :func:`verify`'s, and for the same
    reason — this is reached by anyone holding the URL. Anything that is not
    lowercase SHA-256 hex is ``False`` rather than an exception:
    ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII string, which
    would let an unauthenticated caller turn a 401 into a 500.
    """
    candidate = (header or "").strip()
    if not candidate.startswith(_GITHUB_PREFIX):
        return False
    signature = candidate[len(_GITHUB_PREFIX) :]
    if not _HEX.fullmatch(signature):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def replay_nonce(*, delivery_id: str = "", signature: str = "") -> str | None:
    """What makes one inbound delivery distinguishable from a replay of it.

    Returns ``None`` when the request carries nothing that can serve — which is
    the honest answer for an unsigned request with no delivery id, and the case
    the caller must not silently treat as deduplicated.

    **Why a signed request needs this at all.** ``verify`` bounds a Pulse-signed
    request to a five-minute window, and ``verify_github`` bounds one not at all.
    Inside that window — or, for GitHub, forever — the identical bytes verify
    identically, every time. ``POST /triggers/inbound/{token}`` answers a
    verified request by *writing an article*: a full pass over the LLM chain, a
    link check per outbound link, and a publish dispatch. So anybody who can
    observe one delivery can spend the account's entire daily quota by sending
    it again in a loop, and every copy is a genuinely valid signature.

    **Why not the body.** ``signal_from_webhook`` deliberately leaves
    ``dedupe_key`` empty when the sender configured no ``dedupe_path``, because
    a key invented from the body would make an identical-but-genuine second
    event invisible — two builds of the same commit, two alerts with the same
    text. That reasoning is right and this does not touch it. A delivery id and
    a signature are not the body: a genuine second event carries a fresh
    delivery id, and a fresh signature (different ``t``, or different content).
    Only a literal replay repeats either.

    The delivery id wins when both are present. It is the sender's own name for
    the delivery, so it identifies a redelivery *across* a signature the sender
    recomputed — which is what GitHub's "Redeliver" button does.
    """
    for candidate in (delivery_id, signature):
        value = (candidate or "").strip()
        if value:
            # Hashed rather than stored raw. `TriggerEvent.dedupe_key` is 200
            # characters and a signature header is comfortably under that today,
            # but the value comes from a request header — a caller choosing how
            # long the string in a unique index is is a caller choosing whether
            # the INSERT raises. A digest is the same width whatever arrives.
            return signals.digest_key("replay", value)
    return None


def envelope(event: WebhookEvent, data: dict[str, Any]) -> dict[str, Any]:
    """The body Pulse POSTs, minus the delivery id it gains on insert."""
    return {
        "event": event.value,
        "created_at": utcnow().isoformat(),
        "data": data,
    }


def emit(
    db: Session, *, user_id: int, event: WebhookEvent, data: dict[str, Any]
) -> list[WebhookDelivery]:
    """Queue *event* to every active endpoint of *user_id* subscribed to it.

    Commits, because the caller is usually mid-publish and the delivery rows
    must survive whatever happens to the rest of that transaction. Never raises:
    a webhook is a side channel, and a broken one must not be able to fail a
    publish that already succeeded.
    """
    try:
        hooks = [
            hook
            for hook in db.scalars(
                select(Webhook).where(
                    Webhook.user_id == user_id, Webhook.is_active.is_(True)
                )
            )
            if hook.subscribed_to(event)
        ]
        if not hooks:
            return []

        body = envelope(event, data)
        deliveries = [
            WebhookDelivery(
                webhook_id=hook.id,
                event=event,
                payload=body,
                status=DeliveryStatus.PENDING,
                next_attempt_at=utcnow(),
            )
            for hook in hooks
        ]
        db.add_all(deliveries)
        db.commit()
        for delivery in deliveries:
            db.refresh(delivery)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "failed to queue webhook deliveries for %s (user_id=%s)",
            event,
            user_id,
        )
        db.rollback()
        return []

    dispatch([d.id for d in deliveries])
    return deliveries


def dispatch(delivery_ids: list[int]) -> None:
    """Hand deliveries to a worker, or send them here.

    Mirrors the publish path: the inline branch is for single-process
    deployments and for the case where the broker is unreachable, since a
    dropped notification is exactly the failure a webhook exists to prevent.
    """
    if not delivery_ids:
        return

    from app.tasks import webhook_tasks

    # How many the broker has already accepted. Everything from here on is what
    # the inline fallback is still responsible for — and *only* that. A broker
    # that dies partway through a batch is the exact case this fallback exists
    # for, and starting the inline loop from the top would POST the ids queued
    # before the failure a second time. The receiver cannot tell that duplicate
    # from a genuine retry: same delivery id, same signature, same body.
    dispatched = 0
    if settings.celery_enabled:
        try:
            for delivery_id in delivery_ids:
                webhook_tasks.deliver_one.delay(delivery_id)
                dispatched += 1
            return
        except Exception as exc:
            logger.warning("celery dispatch failed, delivering inline: %s", exc)

    for delivery_id in delivery_ids[dispatched:]:
        try:
            webhook_tasks.deliver_one(delivery_id)
        except Exception:
            # The inline path runs on the caller's thread — usually one that has
            # just published something. The delivery row keeps its state and the
            # sweep will come back for it; the publish must not be affected.
            logger.exception("inline webhook delivery %s failed", delivery_id)


def due_deliveries(db: Session, *, limit: int = 200) -> list[WebhookDelivery]:
    """Pending deliveries whose backoff has elapsed, oldest first."""
    moment = utcnow()
    return list(
        db.scalars(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == DeliveryStatus.PENDING,
                (WebhookDelivery.next_attempt_at.is_(None))
                | (WebhookDelivery.next_attempt_at <= moment),
            )
            .order_by(WebhookDelivery.id)
            .limit(limit)
        )
    )


def claim(db: Session, delivery: WebhookDelivery) -> bool:
    """Take this delivery for one attempt. ``False`` means somebody else has it.

    The row is the lock. A single conditional UPDATE re-checks the same
    condition :func:`due_deliveries` selected on — pending, and due — and moves
    ``next_attempt_at`` a lease into the future, so the loser of a race sees a
    row that is no longer due and returns without sending anything. ``rowcount``
    is the verdict: exactly one caller can observe 1 for a given attempt.

    Nothing before this held that lock. ``due_deliveries`` reads without
    claiming, which is fine for one sweep and wrong for two — and two is the
    default configuration, not an edge case: the beat runs every
    ``webhook_scan_interval_seconds`` (60), one sweep may take up to 200
    deliveries times ``webhook_timeout_seconds`` (10), so a slow round is still
    working when the next one selects the very same pending rows. The same race
    exists between the sweep and the ``deliver_one`` queued at emit time, which
    is a window every single delivery passes through.

    Both duplicates POST the same body under the same delivery id and the same
    signature — indistinguishable, at the receiver, from the retry that id is
    meant to let it collapse. A receiver that dedupes on ``X-Pulse-Delivery``
    is unharmed. One that acts on arrival, which is the reason the header is
    documented rather than assumed, announces twice.

    The attempt counter moves inside the same statement. Whoever wins the claim
    owns the attempt, so the budget cannot be spent twice for one send, and a
    delivery that lost the race does not consume one at all.

    A worker that dies mid-attempt leaves the row pending with the lease still
    on it; it becomes due again when the lease expires, which is what makes
    that the crash-recovery window and not merely a lock timeout.
    """
    # Sessions here are ``autoflush=False``, so an unflushed change to this row
    # would otherwise be invisible to the UPDATE's own WHERE clause and would
    # then be written by the commit below — overwriting the lease the UPDATE
    # just took, which is a claim that silently did not hold. Settle first, so
    # the claim is both informed by and the last word on this row.
    db.flush()

    now = utcnow()
    result = db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == delivery.id,
            WebhookDelivery.status == DeliveryStatus.PENDING,
            (WebhookDelivery.next_attempt_at.is_(None))
            | (WebhookDelivery.next_attempt_at <= now),
        )
        .values(
            attempts=WebhookDelivery.attempts + 1,
            next_attempt_at=now + timedelta(seconds=settings.webhook_claim_lease_seconds),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    # The UPDATE went round the ORM, so the in-memory row still holds the old
    # ``attempts`` and the old ``next_attempt_at``. Everything downstream reads
    # both.
    db.refresh(delivery)
    return result.rowcount == 1


def deliver(db: Session, delivery: WebhookDelivery) -> WebhookDelivery:
    """Attempt one delivery, recording the outcome. Never raises.

    Safe to call for a delivery that is already settled or already in flight:
    the claim below refuses it and nothing is sent.
    """
    try:
        webhook = delivery.webhook
        if webhook is None:  # pragma: no cover - FK cascade makes this unreachable
            _record_failure(db, delivery, "The endpoint no longer exists.", terminal=True)
            return delivery

        if not claim(db, delivery):
            logger.info(
                "webhook delivery %s not claimed (status=%s) — another worker has it "
                "or it is already settled",
                delivery.id,
                delivery.status.value,
            )
            return delivery

        # After the claim, so two workers cannot both settle the same row, and
        # before anything is sent. ``emit`` only *creates* deliveries for active
        # endpoints, but a delivery lives past the moment it was created: one
        # sitting out a backoff when the user switched the endpoint off — or when
        # ``_record_failure`` switched it off for them — was still POSTed by the
        # next sweep, and kept being POSTed until its budget ran out. The account's
        # own flag is the same door: every other worker in the tree checks
        # ``User.is_active`` before acting for an account, and a signed POST to a
        # third party is the one action a deactivated account still had.
        switched_off = _switched_off(webhook)
        if switched_off:
            _record_skipped(db, delivery, switched_off)
            return delivery

        body = _serialize(delivery)

        try:
            url = validate_url(webhook.url)
        except WebhookUrlError as exc:
            # No retry will make a refused URL acceptable, and the user needs to see
            # why rather than watching a counter tick.
            _record_failure(db, delivery, str(exc), terminal=True)
            return delivery

        secret = read_secret(webhook)
        if not secret:
            _record_failure(
                db,
                delivery,
                "The signing secret could not be read — rotate it in Settings.",
                terminal=True,
            )
            return delivery

        timestamp = int(utcnow().timestamp())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"Pulse/0.1 webhooks (+{settings.openrouter_app_url})",
            EVENT_HEADER: delivery.event.value,
            DELIVERY_HEADER: str(delivery.id),
            CORRELATION_HEADER: request_id_var.get(),
            SIGNATURE_HEADER: sign(secret, timestamp, body),
        }

        started = time.monotonic()
        try:
            with _http_client() as client:
                with client.stream("POST", url, content=body.encode("utf-8"), headers=headers) as response:
                    status_code = response.status_code
                    is_redirect = response.is_redirect
                    response_text = _read_capped(response)
        except httpx.HTTPError as exc:
            _record_failure(db, delivery, friendly_network_error(exc))
            return delivery
        except Exception as exc:  # pragma: no cover - defensive
            # "Never raises" has to be true even when the failure is not one httpx
            # models: this runs inline inside a publish that has already succeeded,
            # and an exception here would undo a post that is already live.
            logger.exception("unexpected error delivering webhook %s", delivery.id)
            _record_failure(db, delivery, sanitize_unexpected_error(exc))
            return delivery
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            logger.debug(
                "webhook %s delivery %s POST took %.0fms",
                delivery.webhook_id,
                delivery.id,
                duration_ms,
            )

        delivery.response_status = status_code
        if 200 <= status_code < 300:
            _record_success(db, delivery)
            return delivery

        if is_redirect:
            # See the module docstring: a redirect could be a move or could be a way
            # back inside the network, and Pulse cannot tell which.
            _record_failure(
                db,
                delivery,
                f"Returned {status_code} — Pulse does not follow webhook "
                "redirects. Point the webhook at the final URL.",
                terminal=True,
            )
            return delivery

        _record_failure(
            db,
            delivery,
            f"Returned {status_code}: {_excerpt(response_text)}",
            # 4xx that is not a rate limit is the endpoint saying "not this, ever".
            # Retrying a 401 sixteen times is how a bad secret becomes a log full of
            # noise. 408 and 429 are the two that mean "later", not "no".
            terminal=(
                400 <= status_code < 500
                and status_code not in (408, 429)
            ),
        )
        return delivery
    except Exception as exc:
        db.rollback()
        logger.exception("webhook delivery %s crashed", delivery.id)
        try:
            _record_failure(db, delivery, sanitize_unexpected_error(exc))
        except Exception:
            logger.exception(
                "webhook delivery %s: could not record crash as failure either",
                delivery.id,
            )
        return delivery


_MAX_WEBHOOK_RESPONSE_BYTES = 4096


def _http_client() -> httpx.Client:
    """The client every delivery goes out on.

    A function rather than an inline constructor so the test suite can hand
    ``deliver`` a transport instead of a socket. ``follow_redirects=False`` is
    not a default worth overriding anywhere: see the module docstring.
    """
    return httpx.Client(
        timeout=settings.webhook_timeout_seconds, follow_redirects=False
    )


def _serialize(delivery: WebhookDelivery) -> str:
    """The exact bytes to sign and send, with the delivery id folded in.

    Compact separators and no key sorting beyond insertion order: the signature
    is over this string, so it has to be produced the same way every time,
    including on a retry from a different process.
    """
    body = dict(delivery.payload or {})
    body["id"] = delivery.id
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _read_capped(response: httpx.Response) -> str:
    """Read at most :data:`_MAX_WEBHOOK_RESPONSE_BYTES` from a streaming response.

    A webhook endpoint is user-supplied. Without a cap, a malicious endpoint
    could return an unbounded body and exhaust worker memory before the timeout
    fires.  Only the first few kilobytes matter — :func:`_excerpt` truncates to
    200 characters anyway.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_WEBHOOK_RESPONSE_BYTES:
            break
    raw = b"".join(chunks)[:_MAX_WEBHOOK_RESPONSE_BYTES]
    return raw.decode("utf-8", "replace")


def _excerpt(text: str, limit: int = 200) -> str:
    """A response body short enough to store on the delivery row."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _record_success(db: Session, delivery: WebhookDelivery) -> None:
    now = utcnow()
    delivery.status = DeliveryStatus.DELIVERED
    delivery.delivered_at = now
    delivery.next_attempt_at = None
    delivery.error = None

    webhook = delivery.webhook
    webhook.consecutive_failures = 0
    webhook.last_delivery_at = now
    webhook.last_status = delivery.response_status
    webhook.last_error = None

    db.commit()
    logger.info(
        "webhook %s delivered %s (delivery %s)",
        webhook.id,
        delivery.event.value,
        delivery.id,
    )


def _switched_off(webhook: Webhook) -> str:
    """Why nothing should be sent to *webhook* right now, or ``""``.

    Two reasons, one wording each. The endpoint's own flag is the user's
    (or the breaker's) decision about this URL; the owner's flag is the
    account being closed, which has to close every door it opened.
    """
    if not webhook.is_active:
        return "The endpoint is switched off."
    owner = webhook.user
    if owner is None or not owner.is_active:
        return "The account this endpoint belongs to is deactivated."
    return ""


def _record_skipped(db: Session, delivery: WebhookDelivery, reason: str) -> None:
    """Settle a delivery that was never sent, without blaming the endpoint.

    Distinct from :func:`_record_failure` on purpose. That path is for an
    attempt that happened, and a terminal one moves ``consecutive_failures``
    and can trip the breaker — so routing a skip through it would count "we
    chose not to send" as evidence that the endpoint is broken, and a user who
    disabled an endpoint with four deliveries in backoff would find it disabled
    a second time, by Pulse, on re-enabling. Nothing on the endpoint moves
    here; only the delivery is closed, with a reason the log shows.
    """
    delivery.status = DeliveryStatus.FAILED
    delivery.error = reason
    delivery.next_attempt_at = None
    db.commit()
    logger.info(
        "webhook %s delivery %s skipped: %s", delivery.webhook_id, delivery.id, reason
    )


def _record_failure(
    db: Session, delivery: WebhookDelivery, error: str, *, terminal: bool = False
) -> None:
    """Mark this attempt failed, scheduling the next one unless it was the last."""
    spent = delivery.attempts >= settings.webhook_max_attempts
    give_up = terminal or spent

    # Both the attempt row and the endpoint summary quote whatever the remote
    # end said, which for a misbehaving endpoint is an unbounded error page.
    error = clip_error(error)
    delivery.error = error
    if give_up:
        delivery.status = DeliveryStatus.FAILED
        delivery.next_attempt_at = None
    else:
        delivery.status = DeliveryStatus.PENDING
        delivery.next_attempt_at = utcnow() + timedelta(seconds=_backoff(delivery.attempts))

    webhook = delivery.webhook
    webhook.last_delivery_at = utcnow()
    webhook.last_status = delivery.response_status
    webhook.last_error = error
    if give_up:
        # Only a spent delivery counts against the endpoint. Counting every
        # attempt would disable a webhook after two flaky events rather than
        # after a run of genuinely undelivered ones.
        webhook.consecutive_failures = Webhook.consecutive_failures + 1
        db.flush()
        db.refresh(webhook)
        if webhook.consecutive_failures >= settings.webhook_disable_after_failures:
            webhook.is_active = False
            logger.warning(
                "webhook %s deactivated after %d consecutive failed deliveries",
                webhook.id,
                webhook.consecutive_failures,
            )

    db.commit()
    logger.warning(
        "webhook %s delivery %s failed (%s): %s",
        webhook.id,
        delivery.id,
        "final" if give_up else f"attempt {delivery.attempts}",
        error,
    )


def _backoff(attempt: int) -> float:
    """Seconds to wait before attempt *attempt* + 1.

    Doubling from the configured base, capped. No jitter: unlike a platform API,
    a user's own endpoint is not a shared resource that a thundering herd could
    knock over, and a predictable schedule is easier to reason about when
    somebody is watching a delivery list waiting for a retry.
    """
    delay = settings.webhook_retry_backoff_seconds * (2 ** max(0, attempt - 1))
    return float(min(delay, settings.webhook_retry_max_backoff_seconds))


class RequeueError(RuntimeError):
    """Raised when a delivery cannot be requeued in its current state."""


def requeue(db: Session, delivery: WebhookDelivery) -> WebhookDelivery:
    """Re-arm a finished delivery so it will be attempted again.

    The retry budget resets. This is a person saying "the endpoint is fixed
    now", which is exactly the information the automatic backoff does not have.

    Only terminal deliveries (DELIVERED or FAILED) may be requeued. A PENDING
    delivery may be mid-attempt — a worker that has already claimed it is
    sending the POST right now — and resetting its state underneath that worker
    causes a double-delivery and a corrupted attempt counter.
    """
    if delivery.status == DeliveryStatus.PENDING:
        raise RequeueError(
            "This delivery is still being attempted. "
            "Wait for it to finish before redelivering."
        )
    reason = _switched_off(delivery.webhook)
    if reason:
        raise RequeueError(
            f"The endpoint is switched off. Re-enable it before redelivering."
        )
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.error = None
    delivery.response_status = None
    delivery.delivered_at = None
    delivery.next_attempt_at = utcnow()
    db.commit()
    db.refresh(delivery)
    return delivery


__all__ = [
    "CORRELATION_HEADER",
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "RequeueError",
    "WebhookUrlError",
    "claim",
    "deliver",
    "dispatch",
    "due_deliveries",
    "emit",
    "envelope",
    "generate_secret",
    "read_secret",
    "requeue",
    "sign",
    "store_secret",
    "validate_url",
    "verify",
]
