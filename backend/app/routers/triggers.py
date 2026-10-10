"""Trigger management, plus the one public endpoint in the whole API.

``POST /triggers/inbound/{token}`` is unauthenticated on purpose: the sender is
a GitHub Action, a Zapier zap, a status-page integration — something that has a
URL and no way to hold a Pulse bearer token. The token in the path *is* the
credential, which is why it is 256 bits of ``secrets.token_urlsafe`` and why the
endpoint carries its own rate limit rather than relying on the authenticated
surface's.

A trigger may additionally require an HMAC signature, in the same format Pulse
uses for its own outbound webhooks. That upgrade is worth taking whenever the
sender can do it: a URL leaks by being pasted into a chat window, and a
signature makes a leaked URL useless on its own.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.deps import ListOffset, QueryRowId, RowId, get_current_user, owned_project
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import OWNED, ErrorOut, errors
from app.schemas.trigger import (
    ALLOWED_CONFIG,
    REQUIRED_CONFIG,
    TriggerCheckOut,
    TriggerCreate,
    TriggerCreated,
    TriggerEventOut,
    TriggerInboundOut,
    TriggerKindOut,
    TriggerOut,
    TriggerUpdate,
    validate_config,
)
from app.services import triggers as trigger_service
from app.services import webhooks
from app.services.crypto import CredentialEncryptionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/triggers", tags=["triggers"])

#: What each kind is for, in the words the trigger-builder UI shows.
KIND_DESCRIPTIONS: dict[TriggerKind, str] = {
    TriggerKind.GITHUB: (
        "A watched repository shipped commits or cut a release."
    ),
    TriggerKind.WEBHOOK: (
        "Anything that can send an HTTP request POSTs to a URL only this "
        "trigger knows."
    ),
    TriggerKind.RSS: (
        "An RSS or Atom feed gained an entry — a changelog, a status page, a "
        "release feed."
    ),
    TriggerKind.SCHEDULE: (
        "Time passed. No external event: write something every week whether or "
        "not anything happened."
    ),
}

#: Ceiling on triggers per project. Every polled trigger is an outbound request
#: on a fixed cadence, so an unbounded list is an unbounded amount of work.
MAX_TRIGGERS_PER_PROJECT = 20

#: How much of an inbound body is read. The global body-size middleware allows
#: 1 MB; a trigger payload that large is a mistake, and the parsed result would
#: be truncated into the prompt anyway.
MAX_INBOUND_BYTES = 128 * 1024


def _owned(trigger_id: RowId, db: Session, user: User) -> Trigger:
    """Fetch a trigger, 404ing if its project isn't this user's."""
    trigger = db.get(Trigger, trigger_id)
    if trigger is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found."
        )
    # Reuses the project guard so the "same status either way" reasoning in
    # ``deps.owned_project`` covers triggers too.
    owned_project(trigger.project_id, db, user)
    return trigger


def _inbound_url(trigger: Trigger) -> str | None:
    if not trigger.token:
        return None
    return (
        f"{settings.api_base_url}{settings.api_v1_prefix}"
        f"/triggers/inbound/{trigger.token}"
    )


def _to_out(trigger: Trigger) -> TriggerOut:
    return TriggerOut(
        id=trigger.id,
        project_id=trigger.project_id,
        kind=trigger.kind,
        name=trigger.name,
        is_active=trigger.is_active,
        config=dict(trigger.config or {}),
        inbound_url=_inbound_url(trigger),
        has_secret=bool(trigger.encrypted_secret),
        last_checked_at=trigger.last_checked_at,
        last_fired_at=trigger.last_fired_at,
        fire_count=trigger.fire_count,
        consecutive_failures=trigger.consecutive_failures,
        last_error=trigger.last_error,
        created_at=trigger.created_at,
    )


@router.get(
    "/kinds",
    response_model=list[TriggerKindOut],
    summary="Trigger kinds and their config keys",
    responses=errors(status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit(settings.rate_limit_public_read)
def list_kinds(request: Request, response: Response) -> list[TriggerKindOut]:
    """Every kind of trigger and what it needs configuring.

    Reachable without a token, so it carries a limit like the rest of the
    anonymous surface — see :func:`app.routers.webhooks.list_events` for what
    the two slowapi parameters are doing here.
    """
    return [
        TriggerKindOut(
            kind=kind.value,
            label=kind.label,
            description=KIND_DESCRIPTIONS[kind],
            required_config=list(REQUIRED_CONFIG[kind]),
            optional_config=sorted(
                set(ALLOWED_CONFIG[kind]) - set(REQUIRED_CONFIG[kind])
            ),
        )
        for kind in TriggerKind
    ]


@router.get(
    "",
    response_model=list[TriggerOut],
    summary="Your triggers",
    # 404 only on the narrowed form: an unknown `project_id` resolves through
    # the ownership guard rather than quietly returning an empty page.
    responses=errors(*OWNED),
)
def list_triggers(
    response: Response,
    project_id: QueryRowId | None = Query(default=None),
    # MAX_TRIGGERS_PER_PROJECT bounds this per project, but nothing bounds
    # projects per account, so the unnarrowed listing was 20 x however many
    # projects the caller had made. Same contract as /triggers/{id}/events
    # below.
    limit: int = Query(default=100, ge=1, le=500),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TriggerOut]:
    """This user's triggers, optionally narrowed to one project."""
    from app.models.project import Project

    query = (
        select(Trigger)
        .join(Project, Project.id == Trigger.project_id)
        .where(Project.user_id == user.id)
    )
    if project_id is not None:
        # Resolve through the ownership guard so an id belonging to somebody
        # else 404s rather than returning an empty list.
        owned_project(project_id, db, user)
        query = query.where(Trigger.project_id == project_id)

    total = db.scalar(
        select(func.count()).select_from(query.with_only_columns(Trigger.id).subquery())
    )
    response.headers["X-Total-Count"] = str(total or 0)

    rows = db.scalars(query.order_by(Trigger.id).limit(limit).offset(offset))
    return [_to_out(row) for row in rows]


@router.post(
    "",
    response_model=TriggerCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Register a trigger",
    responses=errors(
        *OWNED,
        status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    ),
)
def create_trigger(
    payload: TriggerCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TriggerCreated:
    """Register a trigger. A webhook trigger's secret is in this response only."""
    owned_project(payload.project_id, db, user)

    count = (
        db.scalar(
            select(func.count(Trigger.id)).where(
                Trigger.project_id == payload.project_id
            )
        )
        or 0
    )
    if count >= MAX_TRIGGERS_PER_PROJECT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"At most {MAX_TRIGGERS_PER_PROJECT} triggers per project.",
        )

    trigger = Trigger(
        project_id=payload.project_id,
        kind=payload.kind,
        name=payload.name.strip(),
        config=payload.config,
        is_active=payload.is_active,
        state={},
    )

    # Only an inbound webhook has a URL to be called on, and only it needs a
    # secret. Minting one for a feed poller would be a credential with nothing
    # on the other end of it.
    secret = ""
    if payload.kind == TriggerKind.WEBHOOK:
        trigger.token = trigger_service.generate_token()
        secret = trigger_service.generate_secret()
        trigger.encrypted_secret = _stored(secret)

    db.add(trigger)
    db.commit()
    db.refresh(trigger)
    return TriggerCreated(**_to_out(trigger).model_dump(), secret=secret)


@router.patch(
    "/{trigger_id}",
    response_model=TriggerOut,
    summary="Change a trigger",
    responses=errors(*OWNED, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
def update_trigger(
    trigger_id: RowId,
    payload: TriggerUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TriggerOut:
    """Patch one trigger. Omitted fields are left alone.

    A new ``config`` is validated against the trigger's *existing* kind, which
    is not patchable: the config keys a kind requires are the kind, so changing
    one under a config that was written for the other is a new trigger wearing
    an old id.
    """
    trigger = _owned(trigger_id, db, user)
    kind = (
        trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)
    )

    if payload.name is not None:
        trigger.name = payload.name.strip()
    if payload.config is not None:
        try:
            trigger.config = validate_config(kind, payload.config)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
    if payload.is_active is not None:
        trigger.is_active = payload.is_active
        if payload.is_active:
            # Re-enabling is the user saying the source is fixed. Leaving the
            # counter where it was would disable it again on the next failure.
            trigger.consecutive_failures = 0
            trigger.last_error = None

    db.commit()
    db.refresh(trigger)
    return _to_out(trigger)


@router.delete(
    "/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a trigger",
    responses=errors(*OWNED),
)
def delete_trigger(
    trigger_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Remove a trigger and its firing history.

    The content it caused to be written stays: a piece that shipped is not a
    fact about the trigger any more.
    """
    trigger = _owned(trigger_id, db, user)
    events = (
        db.scalar(
            select(func.count(TriggerEvent.id)).where(
                TriggerEvent.trigger_id == trigger.id
            )
        )
        or 0
    )
    logger.info(
        "trigger %s (%s/%s) deleted by user %s — %s event(s) went with it",
        trigger.id,
        trigger.kind.value if hasattr(trigger.kind, "value") else trigger.kind,
        trigger.name,
        user.id,
        events,
    )
    db.delete(trigger)
    db.commit()


@router.post(
    "/{trigger_id}/rotate-secret",
    response_model=TriggerCreated,
    summary="Issue a new secret and inbound URL",
    responses=errors(
        *OWNED,
        status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    ),
)
def rotate_secret(
    trigger_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TriggerCreated:
    """Issue a new signing secret and a new inbound URL.

    Both change together on purpose: rotating is what you do after a leak, and
    a leaked URL that keeps working is half a rotation.
    """
    trigger = _owned(trigger_id, db, user)
    kind = (
        trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)
    )
    if kind != TriggerKind.WEBHOOK:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only inbound webhook triggers have a secret.",
        )

    secret = trigger_service.generate_secret()
    trigger.token = trigger_service.generate_token()
    trigger.encrypted_secret = _stored(secret)
    db.commit()
    db.refresh(trigger)
    logger.info(
        "trigger %s (%s/%s) secret rotated by user %s",
        trigger.id,
        kind.value,
        trigger.name,
        user.id,
    )
    return TriggerCreated(**_to_out(trigger).model_dump(), secret=secret)


@router.post(
    "/{trigger_id}/check",
    response_model=TriggerCheckOut,
    summary="Poll this trigger now",
    responses=errors(
        *OWNED,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_outbound_probe, key_func=account_key)
def check_now(
    trigger_id: RowId,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TriggerCheckOut:
    """Poll this trigger now, ignoring its schedule.

    Synchronous, like the outbound webhook's ping and for the same reason: the
    question the button asks is "does my feed URL work?", and an answer that
    arrives in a list a minute later does not answer it.
    """
    trigger = _owned(trigger_id, db, user)
    kind = (
        trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)
    )
    if kind == TriggerKind.WEBHOOK:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An inbound webhook trigger fires when something POSTs to it.",
        )
    return TriggerCheckOut(**trigger_service.check(db, trigger))


def _event_out(event: TriggerEvent, *, payload: bool) -> TriggerEventOut:
    """One event, with or without its frozen signal.

    Built field by field rather than through ``model_validate``: pydantic's
    ``from_attributes`` reads every field it declares, and reading a deferred
    column is a SELECT. Validating a listing of 200 deferred events would
    undefer all 200 of them one at a time — slower than never deferring at all,
    and invisible in the response.
    """
    return TriggerEventOut(
        id=event.id,
        trigger_id=event.trigger_id,
        headline=event.headline,
        status=event.status,
        detail=event.detail,
        content_id=event.content_id,
        dedupe_key=event.dedupe_key,
        payload=dict(event.payload or {}) if payload else None,
        created_at=event.created_at,
    )


@router.get(
    "/{trigger_id}/events",
    response_model=list[TriggerEventOut],
    summary="Firing history for one trigger",
    responses=errors(*OWNED),
)
def list_trigger_events(
    trigger_id: RowId,
    response: Response,
    event_status: TriggerEventStatus | None = Query(default=None, alias="status"),
    # Off by default, which is the change of contract worth naming. An event's
    # payload is the whole inbound body — up to MAX_INBOUND_BYTES, 128 KB — and
    # this endpoint pages 200 of them, so the activity list was a 25 MB response
    # in the worst case to render 200 one-line rows that never touched it.
    # Turning it on gets the old shape back; the per-event endpoint below is the
    # better answer for "what did this one firing carry?", because it is bounded
    # by one payload rather than by ``limit``.
    include_payload: bool = Query(
        default=False,
        description=(
            "Include each event's frozen signal payload. Off by default: a "
            "payload can be 128 KB and this endpoint returns up to 200 events. "
            "Omitted payloads come back as null, which is distinct from the "
            "empty object a firing that carried nothing really has."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TriggerEventOut]:
    """Recent firings, newest first. Payloads only on request."""
    trigger = _owned(trigger_id, db, user)

    query = select(TriggerEvent).where(TriggerEvent.trigger_id == trigger.id)
    if event_status is not None:
        query = query.where(TriggerEvent.status == event_status)

    total = db.scalar(
        select(func.count()).select_from(
            query.with_only_columns(TriggerEvent.id).subquery()
        )
    )
    response.headers["X-Total-Count"] = str(total or 0)

    query = query.order_by(TriggerEvent.id.desc()).limit(limit).offset(offset)
    if not include_payload:
        # The column has to leave the SELECT, not just the response: dropping it
        # after the fact would still have carried every byte across the wire.
        query = query.options(defer(TriggerEvent.payload))

    return [
        _event_out(row, payload=include_payload) for row in db.scalars(query)
    ]


@router.get(
    "/{trigger_id}/events/{event_id}",
    response_model=TriggerEventOut,
    summary="One firing, including its payload",
    responses=errors(*OWNED),
)
def get_trigger_event(
    trigger_id: RowId,
    event_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TriggerEventOut:
    """One firing and the signal it carried.

    What the list stopped including, asked for one event at a time. "Why did
    this fire?" is a question about a single row that somebody clicked, so the
    response is bounded by one payload rather than by the page size.

    The event must belong to the trigger in the path as well as to the caller:
    an id from another trigger 404s rather than resolving, so the URL cannot be
    used to walk somebody else's history through a trigger you do own.
    """
    trigger = _owned(trigger_id, db, user)
    event = db.scalar(
        select(TriggerEvent).where(
            TriggerEvent.id == event_id, TriggerEvent.trigger_id == trigger.id
        )
    )
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trigger event not found."
        )
    return _event_out(event, payload=True)


@router.post(
    "/inbound/{token}",
    response_model=TriggerInboundOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fire an inbound webhook trigger",
    responses={
        **errors(
            status.HTTP_413_CONTENT_TOO_LARGE,
            status.HTTP_429_TOO_MANY_REQUESTS,
        ),
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorOut,
            "description": (
                "No trigger has this token — or it has one and is deactivated. "
                "Deliberately the same answer: a caller holding a URL should "
                "not be able to learn that it once worked."
            ),
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorOut,
            "description": (
                "This trigger requires a signature and the request did not "
                "carry a valid one. Never returned by a trigger that does not "
                "require signatures."
            ),
        },
    },
)
@limiter.limit(settings.rate_limit_trigger_inbound)
async def inbound(
    request: Request,
    response: Response,
    token: str = Path(max_length=64),
    db: Session = Depends(get_db),
) -> TriggerInboundOut:
    """Fire an inbound webhook trigger. **Unauthenticated** — see the module docstring.

    Answers 202 for anything it accepts, including a duplicate, because the
    caller is a machine that will retry a non-2xx and a retry of a duplicate is
    exactly the thing dedupe exists to absorb.

    The only ``async def`` endpoint in the API, and it is one under protest: it
    is written this way because reading the raw body needs an ``await``, and for
    no other reason. Everything a firing actually *does* is synchronous and
    slow — a ``SELECT`` for the trigger, then :func:`triggers.fire`, which
    writes a piece through :mod:`app.services.content_pipeline`: blocking
    ``httpx.post`` calls to the LLM chain, up to ``llm_max_attempts`` sweeps of
    four providers with a rate-limit sleep of up to
    ``llm_retry_max_backoff_seconds`` in between, then a link check per outbound
    link, then publish dispatch.

    Awaited straight from the handler, all of that runs *on the event loop*, and
    a coroutine that blocks the loop does not slow one request down — it stops
    the worker. Nothing else that process is serving gets a byte until the model
    answers: not a dashboard, not a login, not Caddy's ``/health`` poll, which is
    how "somebody's CI job fired a webhook" becomes "the site is down" and then
    an unhealthy backend. Two uvicorn workers means two concurrent firings to
    stall the whole deployment, and the endpoint that starts it is the one
    anybody holding a URL can call.

    Every other route in this app is a plain ``def``, which is what puts it on
    Starlette's worker threads. :func:`_ingest` is the same arrangement, made
    explicit: the loop reads the body and hands off, and the slow half runs
    where the rest of the app runs.

    The checks stay in the order they were in — unknown token before oversized
    body before bad signature — so the answer to any given request is unchanged.
    Only the thread it is computed on is different.
    """
    raw = await request.body()
    # Starlette's header mapping is already case-insensitive, so one read per
    # header is enough. Both signature schemes are collected here rather than in
    # `_ingest` because this is the only frame that has the request.
    headers = _InboundHeaders(
        pulse_signature=request.headers.get(webhooks.SIGNATURE_HEADER) or "",
        github_signature=request.headers.get(webhooks.GITHUB_SIGNATURE_HEADER) or "",
        delivery_id=request.headers.get(webhooks.GITHUB_DELIVERY_HEADER) or "",
    )
    result = await run_in_threadpool(_ingest, db, token, raw, headers)
    return TriggerInboundOut(**result)


@dataclass(frozen=True)
class _InboundHeaders:
    """The headers one inbound firing is authenticated and deduplicated by.

    A record rather than three positional arguments: ``_ingest`` is called
    across a thread boundary and two of these are hex strings of similar
    length, which is the shape of argument list that gets transposed once and
    then verifies signatures against the wrong scheme forever.
    """

    pulse_signature: str = ""
    github_signature: str = ""
    delivery_id: str = ""


def _verified_signature(trigger: Trigger, raw: bytes, headers: _InboundHeaders) -> str:
    """The signature that authenticated this request, or ``""`` if none did.

    Both schemes are accepted and either is sufficient. Pulse's own
    (``X-Pulse-Signature``) is what its docs tell an integrator to send;
    GitHub's (``X-Hub-Signature-256``) is what GitHub sends and cannot be talked
    out of, and a GitHub push event is the most likely thing this endpoint ever
    receives. Refusing the latter meant a GitHub webhook could only be wired up
    with ``require_signature`` off.

    Pulse's is checked first because it is the stricter of the two — it carries
    a timestamp and so is bounded to a five-minute window on its own, where
    GitHub's is bounded by nothing but the replay nonce.

    The *value* comes back rather than a boolean because the caller needs it:
    a verified signature is what :func:`app.services.webhooks.replay_nonce`
    turns into a replay guard, and returning ``True`` would throw away the only
    unforgeable thing about the request.
    """
    secret = trigger_service.read_secret(trigger)
    if not secret:
        return ""
    # Decoded once, here, and only for Pulse's scheme — which signs the decoded
    # form. GitHub's signs the bytes; see `verify_github` on why the two must
    # not share an input.
    if headers.pulse_signature and webhooks.verify(
        secret, headers.pulse_signature, raw.decode("utf-8", errors="replace")
    ):
        return headers.pulse_signature
    if headers.github_signature and webhooks.verify_github(
        secret, headers.github_signature, raw
    ):
        return headers.github_signature
    return ""


def _ingest(db: Session, token: str, raw: bytes, headers: _InboundHeaders) -> dict:
    """One inbound firing, start to finish, off the event loop.

    Split out of :func:`inbound` rather than inlined there because that is the
    whole point: this function is the blocking half, and it has to be callable
    from a worker thread. ``HTTPException`` raised in here propagates out of
    ``run_in_threadpool`` unchanged, so the status codes are the handler's own.
    """
    trigger = db.scalar(select(Trigger).where(Trigger.token == token))
    if trigger is None or not trigger.is_active:
        # Same answer for "no such token" and "deactivated": a caller holding a
        # URL should not be able to learn that it once existed.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active trigger matches this URL. Check the webhook URL "
            "in your sender's configuration, or re-enable the trigger in Pulse.",
        )

    if len(raw) > MAX_INBOUND_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Payload larger than {MAX_INBOUND_BYTES // 1024} KB.",
        )

    # Attempted whether or not the trigger *requires* one, because a signature
    # that verifies is a replay nonce whether or not it was demanded — and an
    # unsigned trigger that a sender chose to sign anyway should get the
    # protection it paid for. Never a reason to reject on its own: only the
    # `require_signature` branch below turns a missing one into a 401.
    signature = _verified_signature(trigger, raw, headers)
    if trigger.setting("require_signature") and not signature:
        logger.info(
            "trigger %s: inbound delivery %s rejected — no valid signature",
            trigger.id,
            headers.delivery_id or "-",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This trigger requires a valid signature. Send an "
            "X-Pulse-Signature header (HMAC-SHA256 with the trigger's secret) "
            "or an X-Hub-Signature-256 header (GitHub webhook format).",
        )

    body_text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(body_text) if body_text.strip() else {}
    except json.JSONDecodeError:
        # Not every sender speaks JSON, and a form post or a plain-text alert is
        # still news. It becomes the summary rather than being refused.
        payload = {"text": body_text[:10000]}

    signal = trigger_service.signal_from_webhook(trigger, payload)
    # Replay protection, layered *under* the sender's own dedupe rather than
    # over it. A trigger with `dedupe_path` configured has told Pulse what
    # makes its events unique and that answer wins; this only fills the gap
    # where there was no answer at all, which is the default and was therefore
    # every trigger nobody had configured. See `webhooks.replay_nonce` for why
    # a nonce can be used here when a body digest cannot.
    if signal.dedupe_key is None:
        signal = replace(
            signal,
            dedupe_key=webhooks.replay_nonce(
                delivery_id=headers.delivery_id, signature=signature
            ),
        )

    event = trigger_service.fire(db, trigger, signal)
    if event is None:
        # Logged, not silent. "The webhook fired and nothing happened" is the
        # support question this endpoint generates, and a delivery absorbed as a
        # duplicate is the answer to most of them — but the 202 the sender gets
        # is deliberately identical to an accepted one, so the log is the only
        # place the difference is recorded.
        logger.info(
            "trigger %s: inbound delivery %s deduplicated",
            trigger.id,
            headers.delivery_id or "-",
        )
        return {"status": "duplicate"}
    logger.info(
        "trigger %s: inbound delivery %s accepted as event %s (%s)",
        trigger.id,
        headers.delivery_id or "-",
        event.id,
        event.status.value,
    )
    return {
        "status": event.status.value,
        "event_id": event.id,
        "content_id": event.content_id,
    }


def _stored(secret: str) -> str:
    try:
        return trigger_service.store_secret(secret)
    except CredentialEncryptionError as exc:
        logger.error("trigger secret encryption failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not encrypt the trigger secret for storage. "
            "Check the server's encryption configuration.",
        ) from exc
