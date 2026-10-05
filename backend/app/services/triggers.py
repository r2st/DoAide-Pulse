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
from app.models.user import User
from app.services import content_pipeline, feeds, signals
from app.services.crypto import (
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.services.errors import clip_error, sanitize_unexpected_error
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
    """Encrypt a signing secret for storage. The inverse of :func:`read_secret`."""
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

    A schedule pinned to an hour counts **days**, not hours, whenever the
    interval is a whole number of them. Elapsed-hours drifts and the pinned hour
    is what it drifts out of: ``last_fired_at`` is stamped when the worker runs,
    which is the poll instant plus however long generating a piece took, so each
    firing lands a little later than the last. "Every 24 hours at 09:00" then
    needs a poll at 09:00:45, gets one at 09:10, and next time needs 09:10:45 —
    marching forward until it runs out of pinned hour, skips that day entirely,
    and resets a poll interval later. At a ten-minute sweep that is one missed
    day in seven, silently, on the trigger whose whole promise is "daily".
    Comparing calendar days removes the accumulator: the hour gate already
    allows only one firing window a day, so the day count is the schedule.
    """
    now = moment or utcnow()
    kind = trigger.kind if isinstance(trigger.kind, TriggerKind) else TriggerKind(trigger.kind)

    if kind == TriggerKind.SCHEDULE:
        hour = trigger.setting("hour_utc")
        pinned: int | None = None
        if hour is not None:
            try:
                pinned = int(hour)
            except (TypeError, ValueError):
                # Junk pins nothing rather than pinning "never" — see
                # ``test_an_uninterpretable_hour_utc_is_ignored_rather_than...``.
                pinned = None
        if pinned is not None and now.hour != pinned:
            return False

        last = trigger.last_fired_at
        hours = interval_hours(trigger)
        # Whole days only. A 36-hour interval has no day count to compare and an
        # unpinned one has no gate keeping it to one firing a day, so both keep
        # the elapsed-hours reading they already had.
        if pinned is not None and hours >= 24 and hours % 24 == 0:
            if last is None:
                return True
            return (now.date() - as_aware(last).date()).days >= round(hours / 24)
        window = timedelta(hours=hours)
    else:
        last = trigger.last_checked_at
        window = timedelta(hours=interval_hours(trigger))

    # ``as_aware`` because SQLite hands back naive datetimes for a column
    # Postgres returns aware ones for, and this comparison runs on both.
    return last is None or (now - as_aware(last)) >= window


def due_triggers(db: Session, *, limit: int = 200) -> list[Trigger]:
    """Active polled triggers on active projects of active users, oldest first.

    The user's own flag matters as much as the project's. Deactivating an
    account stops it signing in (:func:`app.deps.get_current_user`) but stopped
    nothing it had already set running: its triggers kept polling, writing and
    publishing with its stored platform credentials, on a schedule nobody could
    log in to change.
    """
    rows = db.scalars(
        select(Trigger)
        .join(Project, Project.id == Trigger.project_id)
        .join(User, User.id == Project.user_id)
        .where(
            Trigger.is_active.is_(True),
            Project.is_active.is_(True),
            User.is_active.is_(True),
            Trigger.kind != TriggerKind.WEBHOOK,
        )
        .order_by(Trigger.last_checked_at.is_(None).desc(), Trigger.last_checked_at)
        .limit(limit)
    )
    return [t for t in rows if is_due(t)]


# --------------------------------------------------------------------------- #
# Firing                                                                       #
# --------------------------------------------------------------------------- #


def _daily_count(
    db: Session, project_id: int, *, exclude_event_id: int | None = None
) -> int:
    """Pieces this project has had written for it by a trigger in 24 hours.

    Counts both GENERATED events (completed) and RECEIVED events (in-flight).
    Without RECEIVED, concurrent fires all see a count of zero and race past
    the limit — the events are committed as RECEIVED before generation starts,
    but the old query only counted GENERATED.

    *exclude_event_id* keeps the caller's own event out of the count, since
    that event was committed by ``record`` moments ago and has not generated
    anything yet.
    """
    since = utcnow() - timedelta(days=1)
    conditions = [
        Trigger.project_id == project_id,
        TriggerEvent.created_at >= since,
        TriggerEvent.status.in_(
            (TriggerEventStatus.GENERATED, TriggerEventStatus.RECEIVED)
        ),
    ]
    if exclude_event_id is not None:
        conditions.append(TriggerEvent.id != exclude_event_id)
    return (
        db.scalar(
            select(func.count(TriggerEvent.id))
            .join(Trigger, Trigger.id == TriggerEvent.trigger_id)
            .where(*conditions)
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
    try:
        event = record(db, trigger, signal)
        if event is None:
            return None

        trigger.last_fired_at = utcnow()
        trigger.fire_count += 1
        db.commit()

        project = db.get(Project, trigger.project_id)
        if project is None or not project.is_active:
            return _skip(db, event, "The project is paused.")
        # The inbound endpoint is unauthenticated — the token in the URL is the
        # credential — so this is the only place a deactivated account's webhook
        # trigger can be stopped. Same wording as the paused project: a caller
        # holding the URL learns that nothing was written, not why.
        if project.user is None or not project.user.is_active:
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
        if _daily_count(db, project.id, exclude_event_id=event.id) >= settings.trigger_daily_content_limit:
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
                    # The link back to the firing, and the only one that exists in
                    # that direction: ``TriggerEvent.content_id`` is written two
                    # statements below this call, in a *later* transaction, so a
                    # worker that dies in between leaves a committed piece with
                    # nothing pointing at it and a firing that never names it.
                    # Written here, inside the transaction that stores the piece,
                    # it cannot come apart — which is what lets
                    # :func:`reclaim_stuck_events` tell a firing that was
                    # interrupted after the work from one interrupted before it.
                    "event_id": event.id,
                    "trigger_id": trigger.id,
                    "trigger_kind": signal.kind.value,
                    "trigger_name": trigger.name,
                    "headline": signal.headline,
                    "url": signal.url,
                },
            )
        except Exception as exc:
            # Rollback before the log line, not after. ``generate_and_route`` commits,
            # and a commit that fails leaves the session unable to emit SQL — so
            # reading ``trigger.id`` to name the trigger raised ``PendingRollbackError``
            # from inside this handler. That escaped the whole of ``_generate_for``,
            # which meant the event stayed ``pending`` forever: the two lines below,
            # the only thing that ever records a generation failure on it, were never
            # reached.
            db.rollback()
            logger.exception("trigger %s failed while writing: %s", trigger.id, exc)
            event.status = TriggerEventStatus.FAILED
            event.detail = f"Generation failed: {sanitize_unexpected_error(exc)}"
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
    except Exception:
        db.rollback()
        logger.exception("trigger %s: firing crashed", trigger.id)
        return None


# --------------------------------------------------------------------------- #
# Firings nobody finished                                                      #
# --------------------------------------------------------------------------- #


#: What a reclaimed firing says about itself when the piece survived.
ADOPTED_DETAIL = (
    "Written, but Pulse was interrupted before it recorded the outcome. "
    "The piece below was matched back to this firing by a later sweep."
)
#: …and when it did not.
ABANDONED_DETAIL = (
    "Pulse was interrupted while acting on this firing and nothing was "
    "written. It will not be retried: the firing's dedupe key is already "
    "spent, so re-sending the same event is ignored as a duplicate."
)


def reclaim_stuck_events(db: Session, *, now: Any = None) -> int:
    """Settle firings abandoned mid-generation. Returns how many. Commits.

    :func:`record` commits the event at ``received`` before a single word is
    written, which is what makes the dedupe key a promise: the moment the row
    exists, that feed entry, webhook delivery or commit range can never start a
    second piece. :func:`fire` then moves the row to ``generated``, ``skipped``
    or ``failed`` — and it is the *only* thing in Pulse that ever moves it.

    So a process that dies in between (OOM, a deploy restarting the worker,
    ``check_trigger``'s hard time limit, an API worker recycled mid-request on
    the inbound webhook path) leaves the row at ``received`` permanently. That
    is worse than an unfinished job, because of what the dedupe key already
    promised: nothing retries the firing, and nothing *can*, since the sender's
    redelivery and the next feed poll both collide with the key and are recorded
    as duplicates. The news is gone, and the only trace is a row that reads
    "Fired." in the UI for as long as it is kept.

    Two outcomes, and which one a firing gets is a question of fact rather than
    of guesswork, because :func:`fire` stamps the event's id into the piece's
    ``source`` inside the transaction that stores it:

    * the piece exists — the crash landed between storing it and recording that
      it had been stored. The firing is ``generated`` and points at it, which is
      what it would have said had the worker lived. Getting this right matters
      beyond tidiness: the piece may well have auto-published, and calling that
      firing "failed" would be a plain untruth in the one place a user looks.
    * nothing was written. The firing is ``failed``, and says so — a firing that
      is visibly lost is worth much more than one that looks pending forever,
      and being terminal it also becomes eligible for the retention sweep.

    Deliberately not a retry. The publication sweep re-arms what it reclaims
    because a publish has an attempt counter to spend; a firing has none, and
    re-running a generation that has already killed one worker would cycle for
    as long as the payload keeps doing it — with a model call each time. One
    death is enough evidence to hand this to a human.

    The cutoff is what keeps a *live* generation out of the query — see
    ``settings.trigger_event_stuck_after_seconds``. ``created_at`` is the right
    clock: a ``received`` row is never updated, so its age is the age of the
    attempt.
    """
    from app.models.content import Content  # avoid a circular import

    cutoff = (now or utcnow()) - timedelta(
        seconds=settings.trigger_event_stuck_after_seconds
    )
    stuck = list(
        db.scalars(
            select(TriggerEvent)
            .where(
                TriggerEvent.status == TriggerEventStatus.RECEIVED,
                TriggerEvent.created_at <= cutoff,
            )
            .order_by(TriggerEvent.id)
        )
    )
    if not stuck:
        return 0

    # One statement for the whole batch rather than one per row: the moment
    # this sweep has work to do is the moment something is already wrong, and a
    # deploy that restarted the worker mid-sweep leaves a row per trigger.
    written = dict(
        db.execute(
            select(Content.source["event_id"].as_integer(), Content.id).where(
                Content.source["event_id"].as_integer().in_([e.id for e in stuck])
            )
        ).all()
    )

    adopted = 0
    for event in stuck:
        content_id = written.get(event.id)
        if content_id is not None:
            event.status = TriggerEventStatus.GENERATED
            event.content_id = content_id
            event.detail = ADOPTED_DETAIL
            adopted += 1
        else:
            event.status = TriggerEventStatus.FAILED
            event.detail = ABANDONED_DETAIL
    db.commit()

    # Counted above rather than read back off the rows: the commit expires every
    # one of them, so asking a settled event what its status is costs a SELECT
    # per row — paid at the moment something has already gone wrong, which is
    # the worst moment to be paying it.
    logger.warning(
        "settled %d trigger firing(s) abandoned mid-generation "
        "(%d had a piece to adopt)",
        len(stuck),
        adopted,
    )
    return len(stuck)


# --------------------------------------------------------------------------- #
# Checking one trigger                                                         #
# --------------------------------------------------------------------------- #


def _mark_checked(db: Session, trigger: Trigger, error: str | None = None) -> None:
    trigger.last_checked_at = utcnow()
    # A feed or repo that answers with a wall of HTML instead of what it
    # promised puts that whole body in the exception's message, and this row is
    # rewritten on every poll.
    trigger.last_error = clip_error(error) if error else None
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
    try:
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
        except Exception as exc:
            # The checks above write — an RSS or GitHub poll that finds news records
            # a ``TriggerEvent`` and commits — so this handler can be entered with a
            # session that has a failed flush behind it and will refuse to emit SQL.
            # Without the rollback, ``_mark_checked``'s own commit raised
            # ``PendingRollbackError`` on the way out, so the error this arm exists to
            # record was never written to the row and never counted against
            # ``consecutive_failures``: a trigger failing this way could not reach the
            # threshold that deactivates it, and the sweep saw the raise instead.
            db.rollback()
            logger.exception("trigger %s crashed: %s", trigger.id, exc)
            _mark_checked(db, trigger, sanitize_unexpected_error(exc))
            return {"trigger_id": trigger.id, "status": "error", "error": sanitize_unexpected_error(exc)}

        _mark_checked(db, trigger)
        return {"trigger_id": trigger.id, **result}
    except Exception:
        db.rollback()
        logger.exception("trigger %s: post-check bookkeeping failed", trigger.id)
        return {"trigger_id": trigger.id, "status": "error", "error": "Trigger check completed but results could not be saved. Try again shortly."}


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

    below_threshold = False
    if not first_scan and activity.has_news:
        threshold = trigger.setting("commit_threshold")
        minimum = (
            int(threshold)
            if isinstance(threshold, (int, float)) and int(threshold) > 0
            else settings.autopilot_commit_threshold
        )
        below_threshold = not activity.new_release and len(activity.new_commits) < minimum

    # Below the bar the commits are still news, just not enough of it yet, so
    # the watermark must hold — the same commits get re-read and can
    # accumulate across scans until they clear it. Advancing past them here
    # means "not enough happened this scan" rather than "not enough has
    # happened yet", and against an hourly poll a repo pushed at any human
    # rate is refused every hour and never accumulates. Same bug, same fix, as
    # app.tasks.autopilot_tasks — this is the other place a GitHub watermark
    # advances.
    if not below_threshold:
        state["last_sha"] = activity.head_sha
        state["last_tag"] = activity.latest_tag
        trigger.state = state
    db.commit()

    if first_scan:
        return {"status": "baselined", "commits": len(activity.new_commits)}
    if not activity.has_news:
        return {"status": "no_news"}
    if below_threshold:
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
    "ABANDONED_DETAIL",
    "ADOPTED_DETAIL",
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
    "reclaim_stuck_events",
    "record",
    "signal_from_entries",
    "signal_from_schedule",
    "signal_from_webhook",
    "store_secret",
]
