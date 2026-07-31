"""Outbound webhook management.

CRUD plus the two things that make a webhook debuggable without server access:
a test delivery you can fire on demand, and the delivery log with a redeliver
button on it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.models.webhook import (
    SUBSCRIBABLE_EVENTS,
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.schemas.webhook import (
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryOut,
    WebhookEventOut,
    WebhookOut,
    WebhookUpdate,
)
from app.services import webhooks
from app.services.crypto import CredentialEncryptionError

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: What each event means, for the settings UI. Kept here rather than on the enum
#: because it is copy, and copy belongs next to the endpoint that serves it.
EVENT_DESCRIPTIONS: dict[WebhookEvent, str] = {
    WebhookEvent.CONTENT_PUBLISHED: (
        "A piece went live for the first time, on any platform."
    ),
    WebhookEvent.PUBLICATION_FAILED: (
        "One platform gave up on one piece after its retries were spent."
    ),
    WebhookEvent.REVIEW_PENDING: (
        "The autopilot wrote something and parked it for a human to look at."
    ),
}

#: Ceiling on endpoints per user. Not a licensing decision — every event fans
#: out to every subscribed endpoint, so an unbounded list is an unbounded
#: amount of work per publish.
MAX_WEBHOOKS_PER_USER = 20


def _owned(webhook_id: int, db: Session, user: User) -> Webhook:
    """Fetch a webhook, 404ing if it isn't this user's — see ``deps.owned_project``."""
    webhook = db.get(Webhook, webhook_id)
    if webhook is None or webhook.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found"
        )
    return webhook


@router.get("/events", response_model=list[WebhookEventOut])
def list_events() -> list[WebhookEventOut]:
    """Every event a webhook can subscribe to, and what it means."""
    return [
        WebhookEventOut(event=event.value, description=EVENT_DESCRIPTIONS[event])
        for event in SUBSCRIBABLE_EVENTS
    ]


@router.get("", response_model=list[WebhookOut])
def list_webhooks(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[WebhookOut]:
    rows = db.scalars(
        select(Webhook).where(Webhook.user_id == user.id).order_by(Webhook.id)
    )
    return [WebhookOut.model_validate(row) for row in rows]


@router.post("", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
def create_webhook(
    payload: WebhookCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookCreated:
    """Register an endpoint. The signing secret is in this response and nowhere else."""
    count = len(
        list(db.scalars(select(Webhook.id).where(Webhook.user_id == user.id)))
    )
    if count >= MAX_WEBHOOKS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"At most {MAX_WEBHOOKS_PER_USER} webhooks per account.",
        )

    url = _validated(payload.url)
    secret = webhooks.generate_secret()
    webhook = Webhook(
        user_id=user.id,
        url=url,
        description=payload.description.strip(),
        events=[e.value for e in payload.events],
        encrypted_secret=_stored(secret),
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return WebhookCreated(**WebhookOut.model_validate(webhook).model_dump(), secret=secret)


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: int,
    payload: WebhookUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookOut:
    webhook = _owned(webhook_id, db, user)

    if payload.url is not None:
        webhook.url = _validated(payload.url)
    if payload.events is not None:
        webhook.events = [e.value for e in payload.events]
    if payload.description is not None:
        webhook.description = payload.description.strip()
    if payload.is_active is not None:
        webhook.is_active = payload.is_active
        if payload.is_active:
            # Re-enabling is the user saying the endpoint is fixed. Leaving the
            # counter where it was would disable it again on the next failure.
            webhook.consecutive_failures = 0

    db.commit()
    db.refresh(webhook)
    return WebhookOut.model_validate(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    webhook = _owned(webhook_id, db, user)
    db.delete(webhook)
    db.commit()


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookCreated)
def rotate_secret(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookCreated:
    """Issue a new signing secret. The old one stops verifying immediately."""
    webhook = _owned(webhook_id, db, user)
    secret = webhooks.generate_secret()
    webhook.encrypted_secret = _stored(secret)
    db.commit()
    db.refresh(webhook)
    return WebhookCreated(**WebhookOut.model_validate(webhook).model_dump(), secret=secret)


@router.post("/{webhook_id}/ping", response_model=WebhookDeliveryOut)
def ping(
    webhook_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookDeliveryOut:
    """Send a test delivery now, subscriptions and active flag ignored.

    Deliberately synchronous: the point of the button is to find out whether the
    endpoint works, and a queued answer that arrives in the delivery list a
    minute later does not answer the question that was asked.
    """
    webhook = _owned(webhook_id, db, user)

    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event=WebhookEvent.PING,
        payload=webhooks.envelope(
            WebhookEvent.PING,
            {"webhook_id": webhook.id, "message": "Herald is calling to say hello."},
        ),
        status=DeliveryStatus.PENDING,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    webhooks.deliver(db, delivery)
    db.refresh(delivery)
    return WebhookDeliveryOut.model_validate(delivery)


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryOut])
def list_deliveries(
    webhook_id: int,
    response: Response,
    delivery_status: DeliveryStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[WebhookDeliveryOut]:
    """Recent deliveries, newest first."""
    webhook = _owned(webhook_id, db, user)

    query = select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook.id)
    if delivery_status is not None:
        query = query.where(WebhookDelivery.status == delivery_status)

    total = db.scalar(
        select(func.count()).select_from(
            query.with_only_columns(WebhookDelivery.id).subquery()
        )
    )
    response.headers["X-Total-Count"] = str(total or 0)

    rows = db.scalars(
        query.order_by(WebhookDelivery.id.desc()).limit(limit).offset(offset)
    )
    return [WebhookDeliveryOut.model_validate(row) for row in rows]


@router.post(
    "/{webhook_id}/deliveries/{delivery_id}/redeliver",
    response_model=WebhookDeliveryOut,
)
def redeliver(
    webhook_id: int,
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookDeliveryOut:
    """Send a past delivery again, with a fresh retry budget.

    The body is the one that was frozen at emit time, so this replays the event
    as it happened rather than as things stand now.
    """
    webhook = _owned(webhook_id, db, user)
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.webhook_id != webhook.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found"
        )

    webhooks.requeue(db, delivery)
    webhooks.deliver(db, delivery)
    db.refresh(delivery)
    return WebhookDeliveryOut.model_validate(delivery)


def _validated(url: str) -> str:
    try:
        return webhooks.validate_url(url)
    except webhooks.WebhookUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _stored(secret: str) -> str:
    try:
        return webhooks.store_secret(secret)
    except CredentialEncryptionError as exc:
        # Production without TOKEN_ENCRYPTION_KEY. Refusing is the same answer
        # the platform-credential path gives, for the same reason.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
