"""Firing triggers: turning "something happened" into a piece of content.

One function is the whole point of the module — :func:`fire`, which takes a
trigger and a normalized :class:`~app.services.signals.TriggerSignal` and either
writes something or records why it did not. Everything else here exists to
produce a signal from the four kinds of source, or to stop the same signal from
producing two posts.

Deduplication is where the correctness lives. Inbound webhooks get retried by
the sender, feeds reorder and republish, and a poll that overlaps its
predecessor sees the same entry twice. So every firing carries a dedupe key, the
key is unique per trigger at the database level, and a collision is a normal
outcome recorded as ``skipped`` rather than an error. The alternative — trusting
that a source only ever says a thing once — is how a status-page blip becomes
four identical press releases.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import replace
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import ContentType
from app.models.mixins import as_aware, utcnow
from app.models.project import AutopilotMode, Project
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.services import content_pipeline, feeds, signals
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.services.signals import TriggerSignal

logger = logging.getLogger(__name__)


class TriggerError(RuntimeError):
    """A trigger could not be checked. The message is shown to the user."""


# --------------------------------------------------------------------------- #
# Secrets and tokens                                                           #
# --------------------------------------------------------------------------- #


def generate_token() -> str:
    """The public half of an inbound webhook URL. 256 bits, URL-safe."""
    return secrets.token_urlsafe(32)


def generate_secret() -> str:
    """A fresh HMAC signing secret for an inbound webhook. Shown once."""
    return secrets.token_urlsafe(32)


def store_secret(secret: str) -> str:
    return encrypt_credentials({"secret": secret})


def read_secret(trigger: Trigger) -> str:
    """The signing secret, or ``""`` if it cannot be read.

    Unreadable means the encryption key rotated. That is a reason to refuse an
    inbound request, not to crash: the user rotates the trigger's secret and
    updates the sender.
    """
    if not trigger.encrypted_secret:
        return ""
    try:
        return str(decrypt_credentials(trigger.encrypted_secret).get("secret") or "")
    except CredentialEncryptionError as exc:
        logger.warning("trigger %s secret unreadable: %s", trigger.id, exc)
        return ""


# --------------------------------------------------------------------------- #
# Building signals                                                             #
# --------------------------------------------------------------------------- #


def _dig(payload: Any, path: str) -> Any:
    """Follow a dotted path into a decoded JSON body, or ``None``.

    ``"data.attributes.title"`` and ``"items.0.name"`` both work. Deliberately
    not JSONPath: the config is typed into a text box by somebody looking at a
    sample payload, and a syntax with one rule is one they get right first try.
    """
    current = payload
    for part in (path or "").split("."):
        if not part:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _as_text(value: Any, limit: int = signals.SUMMARY_LIMIT) -> str:
    """Whatever the sender put there, as a string a prompt can hold."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_as_text(item, 300) for item in value[:25])[:limit]
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_as_text(v, 200)}" for k, v in list(value.items())[:25])[
            :limit
        ]
    return str(value)[:limit]


def content_type_for(trigger: Trigger, fallback: ContentType) -> ContentType:
    """The type this trigger writes, or *fallback* when it has no opinion."""
    raw = str(trigger.setting("content_type") or "").strip().lower()
    if not raw:
        return fallback
    try:
        return ContentType(raw)
    except ValueError:
        logger.warning(
            "trigger %s names an unknown content_type %r — using %s",
            trigger.id,
            raw,
            fallback.value,
        )
        return fallback


def signal_from_webhook(trigger: Trigger, body: Any) -> TriggerSignal:
    """Normalize an inbound webhook body using the trigger's field paths.

    Every path is optional. With none configured the whole body becomes the
    summary, which is the right default: a model handed a JSON blob about a
    closed Linear cycle can write about it, and asking the user to map fields
    before their first successful firing is how a setup flow gets abandoned.
    """
    headline_path = str(trigger.setting("headline_path") or "")
    summary_path = str(trigger.setting("summary_path") or "")
    url_path = str(trigger.setting("url_path") or "")
    dedupe_path = str(trigger.setting("dedupe_path") or "")

    headline = _as_text(_dig(body, headline_path), 300) if headline_path else ""
    summary = _as_text(_dig(body, summary_path)) if summary_path else ""
    url = _as_text(_dig(body, url_path), 500) if url_path else ""
    dedupe_raw = _as_text(_dig(body, dedupe_path), 200) if dedupe_path else ""

    if not headline and not summary:
        summary = _as_text(body)
    if not headline:
        headline = f"{trigger.name or 'Webhook'} fired"

    return TriggerSignal(
        kind=TriggerKind.WEBHOOK,
        source=trigger.name or "Inbound webhook",
        headline=headline,
        summary=summary,
        url=url or None,
        # No id in the payload means nothing to compare against, and inventing
        # one from the body would make an identical-but-genuine second event
        # invisible. The sender that wants exactly-once sets dedupe_path.
        dedupe_key=(
            signals.digest_key("webhook", str(trigger.id), dedupe_raw)
            if dedupe_raw
            else None
        ),
        suggested_type=content_type_for(trigger, ContentType.ANNOUNCEMENT),
        raw=body if isinstance(body, dict) else {"body": body},
    )


def signal_from_entries(
    trigger: Trigger, feed: feeds.Feed, entries: list[feeds.FeedEntry]
) -> TriggerSignal | None:
    """One signal covering the new entries of a feed poll.

    One signal rather than one per entry: three changelog entries that appeared
    between two polls are one piece of news, and writing three posts about them
    is how a feed becomes a firehose. The newest entry leads, because it is the
    one a reader would recognise.
    """
    if not entries:
        return None

    lead = entries[0]
    others = entries[1:]
    label = trigger.name or feed.title or "RSS"
    return TriggerSignal(
        kind=TriggerKind.RSS,
        source=f"RSS {label}".strip(),
        headline=lead.title or f"New entry in {label}",
        summary=lead.summary,
        items=tuple(entry.title for entry in others if entry.title),
        item_noun="entry",
        url=lead.link or None,
        dedupe_key=signals.digest_key("rss", str(trigger.id), lead.entry_id),
        suggested_type=content_type_for(trigger, ContentType.ANNOUNCEMENT),
        raw={
            "feed_title": feed.title,
            "entries": [
                {"id": e.entry_id, "title": e.title, "link": e.link} for e in entries
            ],
        },
    )


def signal_from_schedule(trigger: Trigger, *, moment: Any = None) -> TriggerSignal:
    """The signal a schedule tick produces.

    There is no external event, so the "news" is the project itself plus
    whatever standing direction the user left in the config. The dedupe key is
    the slot rather than the instant: two polls inside the same hour must not
    both fire, and the poll interval is deliberately shorter than the smallest
    schedule.
    """
    now = moment or utcnow()
    slot = now.strftime("%Y-%m-%dT%H")
    topic = str(trigger.setting("topic") or "").strip()
    label = trigger.name or "Schedule"

    return TriggerSignal(
        kind=TriggerKind.SCHEDULE,
        source=f"Schedule {label}".strip(),
        headline=topic or f"Scheduled piece: {label}",
        summary=str(trigger.setting("instructions") or "").strip(),
        dedupe_key=signals.digest_key("schedule", str(trigger.id), slot),
        suggested_type=content_type_for(trigger, ContentType.HOW_TO),
        raw={"slot": slot, "topic": topic},
    )


# --------------------------------------------------------------------------- #
# Due-ness                                                                     #
# --------------------------------------------------------------------------- #


def interval_hours(trigger: Trigger) -> float:
    """How often this trigger wants to be checked, in hours."""
    try:
        value = float(trigger.setting("every_hours") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = float(settings.trigger_default_interval_hours)
    return value


def is_due(trigger: Trigger, *, moment: Any = None) -> bool:
    """Whether a polled trigger should be checked now.

    A schedule trigger's window is judged against ``last_fired_at`` — the point
    of "every 168 hours" is a post a week, and a check that finds nothing should
    not reset the clock. Fetch-based kinds are judged against
    ``last_checked_at``, because for them a check *is* the work.
    """
    now = moment or utcnow()
    kind = trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)

    if kind == TriggerKind.SCHEDULE:
        hour = trigger.setting("hour_utc")
        if hour is not None:
            try:
                if now.hour != int(hour):
                    return False
            except (TypeError, ValueError):
                pass
        last = trigger.last_fired_at
        window = timedelta(hours=interval_hours(trigger))
    else:
        last = trigger.last_checked_at
        window = timedelta(hours=interval_hours(trigger))

    # ``as_aware`` because SQLite hands back naive datetimes for a column
    # Postgres returns aware ones for, and this comparison runs on both.
    return last is None or (now - as_aware(last)) >= window


def due_triggers(db: Session, *, limit: int = 200) -> list[Trigger]:
    """Active polled triggers on active projects, oldest check first."""
    rows = db.scalars(
        select(Trigger)
        .join(Project, Project.id == Trigger.project_id)
        .where(
            Trigger.is_active.is_(True),
            Project.is_active.is_(True),
            Trigger.kind != TriggerKind.WEBHOOK,
        )
        .order_by(Trigger.last_checked_at.is_(None).desc(), Trigger.last_checked_at)
        .limit(limit)
    )
    return [t for t in rows if is_due(t)]


# --------------------------------------------------------------------------- #
# Firing                                                                       #
# --------------------------------------------------------------------------- #


def _daily_count(db: Session, project_id: int) -> int:
    """Pieces this project has had written for it by a trigger in 24 hours."""
    since = utcnow() - timedelta(days=1)
    return (
        db.scalar(
            select(func.count(TriggerEvent.id))
            .join(Trigger, Trigger.id == TriggerEvent.trigger_id)
            .where(
                Trigger.project_id == project_id,
                TriggerEvent.created_at >= since,
                TriggerEvent.status == TriggerEventStatus.GENERATED,
            )
        )
        or 0
    )


def record(db: Session, trigger: Trigger, signal: TriggerSignal) -> TriggerEvent | None:
    """Insert the event row for one firing, or ``None`` if it is a duplicate.

    The uniqueness constraint is the authority, not a preceding SELECT: two
    workers polling the same feed is a race the database is built to settle and
    application code is not.
    """
    event = TriggerEvent(
        trigger_id=trigger.id,
        dedupe_key=signal.dedupe_key,
        headline=signal.headline[:300],
        payload=signal.event_payload(),
        status=TriggerEventStatus.RECEIVED,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(
            "trigger %s: duplicate firing %s ignored", trigger.id, signal.dedupe_key
        )
        return None
    db.refresh(event)
    return event


def _skip(db: Session, event: TriggerEvent, reason: str) -> TriggerEvent:
    event.status = TriggerEventStatus.SKIPPED
    event.detail = reason
    db.commit()
    db.refresh(event)
    return event


def fire(db: Session, trigger: Trigger, signal: TriggerSignal) -> TriggerEvent | None:
    """Act on one signal. Returns the event row, or ``None`` if deduplicated.

    Never raises: a trigger is one of many, and a project whose generation fails
    must not stop the sweep for everyone else. The failure is on the event row,
    which is where a user looks for it.
    """
    event = record(db, trigger, signal)
    if event is None:
        return None

    trigger.last_fired_at = utcnow()
    trigger.fire_count += 1
    db.commit()

    project = db.get(Project, trigger.project_id)
    if project is None or not project.is_active:
        return _skip(db, event, "The project is paused.")

    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    if mode == AutopilotMode.OFF:
        return _skip(
            db,
            event,
            "The project's autopilot is off, so the trigger was logged but "
            "nothing was written.",
        )
    if _daily_count(db, project.id) >= settings.trigger_daily_content_limit:
        return _skip(
            db,
            event,
            f"Daily limit of {settings.trigger_daily_content_limit} "
            "trigger-written pieces reached.",
        )
    if not signal.has_news:
        return _skip(db, event, "The signal carried nothing to write about.")

    try:
        routed = content_pipeline.generate_and_route(
            db,
            project,
            content_type=signal.suggested_type,
            signal=signal,
            instructions=str(trigger.setting("instructions") or ""),
            source={
                "kind": "trigger",
                "trigger_id": trigger.id,
                "trigger_kind": signal.kind.value,
                "trigger_name": trigger.name,
                "headline": signal.headline,
                "url": signal.url,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("trigger %s failed while writing: %s", trigger.id, exc)
        db.rollback()
        event.status = TriggerEventStatus.FAILED
        event.detail = f"Generation failed: {exc}"
        db.commit()
        db.refresh(event)
        return event

    event.status = TriggerEventStatus.GENERATED
    event.content_id = routed.content.id
    event.detail = (
        "Published automatically." if routed.auto_published else "Queued for review."
    )
    db.commit()
    db.refresh(event)
    logger.info(
        "trigger %s (%s) wrote content %s: %s",
        trigger.id,
        signal.kind.value,
        routed.content.id,
        routed.status,
    )
    return event


# --------------------------------------------------------------------------- #
# Checking one trigger                                                         #
# --------------------------------------------------------------------------- #


def _mark_checked(db: Session, trigger: Trigger, error: str | None = None) -> None:
    trigger.last_checked_at = utcnow()
    trigger.last_error = error
    if error:
        trigger.consecutive_failures += 1
        if trigger.consecutive_failures >= settings.trigger_disable_after_failures:
            trigger.is_active = False
            logger.warning(
                "trigger %s deactivated after %d consecutive failures",
                trigger.id,
                trigger.consecutive_failures,
            )
    else:
        trigger.consecutive_failures = 0
    db.commit()


def check(db: Session, trigger: Trigger) -> dict[str, Any]:
    """Poll one trigger now and act on whatever it finds. Never raises."""
    kind = trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)
    try:
        if kind == TriggerKind.RSS:
            result = _check_rss(db, trigger)
        elif kind == TriggerKind.GITHUB:
            result = _check_github(db, trigger)
        elif kind == TriggerKind.SCHEDULE:
            result = _check_schedule(db, trigger)
        else:
            return {"trigger_id": trigger.id, "status": "not_polled"}
    except TriggerError as exc:
        _mark_checked(db, trigger, str(exc))
        return {"trigger_id": trigger.id, "status": "error", "error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("trigger %s crashed: %s", trigger.id, exc)
        _mark_checked(db, trigger, f"Unexpected error: {exc}")
        return {"trigger_id": trigger.id, "status": "error", "error": str(exc)}

    _mark_checked(db, trigger)
    return {"trigger_id": trigger.id, **result}


def _check_rss(db: Session, trigger: Trigger) -> dict[str, Any]:
    url = str(trigger.setting("feed_url") or "").strip()
    if not url:
        raise TriggerError("This trigger has no feed URL.")

    try:
        feed = feeds.fetch(url)
    except feeds.FeedError as exc:
        raise TriggerError(str(exc)) from exc

    state = dict(trigger.state or {})
    seen = state.get("seen_ids")
    fresh = feeds.new_entries(feed, seen if isinstance(seen, list) else None)

    # Cap what one poll acts on. A feed that publishes forty entries at once —
    # a backfill, a migration — is not forty pieces of news, and the watermark
    # below still records every id so the rest are never revisited.
    capped = fresh[: settings.feed_max_new_entries]

    state["seen_ids"] = feeds.remember(seen if isinstance(seen, list) else [], feed)
    state["feed_title"] = feed.title
    trigger.state = state
    db.commit()

    if seen is None:
        return {"status": "baselined", "entries": len(feed.entries)}
    signal = signal_from_entries(trigger, feed, capped)
    if signal is None:
        return {"status": "no_news"}

    event = fire(db, trigger, signal)
    return _fired(event, extra={"new_entries": len(fresh)})


def _check_github(db: Session, trigger: Trigger) -> dict[str, Any]:
    from app.services import github_client

    project = db.get(Project, trigger.project_id)
    repo = str(trigger.setting("repo") or "").strip()
    if not repo and project is not None:
        repo = project.repo_full_name or ""
    if not repo:
        raise TriggerError(
            "This trigger has no repository, and its project has no GitHub URL."
        )

    state = dict(trigger.state or {})
    since_sha = state.get("last_sha")
    try:
        activity = github_client.fetch_activity(
            repo, since_sha=since_sha, since_tag=state.get("last_tag")
        )
    except github_client.GitHubRateLimited as exc:
        # Do not move the watermark — we genuinely did not look.
        raise TriggerError(str(exc)) from exc
    except github_client.GitHubError as exc:
        raise TriggerError(str(exc)) from exc

    first_scan = since_sha is None
    state["last_sha"] = activity.head_sha
    state["last_tag"] = activity.latest_tag
    trigger.state = state
    db.commit()

    if first_scan:
        return {"status": "baselined", "commits": len(activity.new_commits)}
    if not activity.has_news:
        return {"status": "no_news"}

    threshold = trigger.setting("commit_threshold")
    minimum = (
        int(threshold)
        if isinstance(threshold, (int, float)) and int(threshold) > 0
        else settings.autopilot_commit_threshold
    )
    if not activity.new_release and len(activity.new_commits) < minimum:
        return {"status": "below_threshold", "commits": len(activity.new_commits)}

    signal = signals.from_repo_activity(activity, source=trigger.name or None)
    signal = replace(
        signal, suggested_type=content_type_for(trigger, signal.suggested_type)
    )
    event = fire(db, trigger, signal)
    return _fired(event, extra={"commits": len(activity.new_commits)})


def _check_schedule(db: Session, trigger: Trigger) -> dict[str, Any]:
    if not is_due(trigger):
        return {"status": "not_due"}
    event = fire(db, trigger, signal_from_schedule(trigger))
    return _fired(event)


def _fired(event: TriggerEvent | None, *, extra: dict[str, Any] | None = None) -> dict:
    """The result dict for a firing, whatever became of it."""
    if event is None:
        return {"status": "duplicate", **(extra or {})}
    body: dict[str, Any] = {
        "status": event.status.value,
        "event_id": event.id,
        **(extra or {}),
    }
    if event.content_id:
        body["content_id"] = event.content_id
    if event.detail:
        body["detail"] = event.detail
    return body


__all__ = [
    "TriggerError",
    "check",
    "content_type_for",
    "due_triggers",
    "fire",
    "generate_secret",
    "generate_token",
    "interval_hours",
    "is_due",
    "read_secret",
    "record",
    "signal_from_entries",
    "signal_from_schedule",
    "signal_from_webhook",
    "store_secret",
]
