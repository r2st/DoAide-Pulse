"""Turning "publish this on Tuesday" into a timestamp a worker will act on.

Herald already stored a ``scheduled_for`` on both content and publications; what
was missing was everything around it. This module owns three jobs:

* **Normalising** what the client sent. A naive datetime is read as UTC — the
  only defensible reading when the API's every other timestamp is UTC — and a
  time in the past or absurdly far ahead is refused rather than silently
  publishing on the next sweep. A mistyped year is the failure mode: ``2025``
  for ``2026`` currently means "publish immediately", which is the one outcome
  somebody setting a date did not want.

* **Choosing** a time when the user would rather not.
  :mod:`app.services.learned_cadence` answers when this user's own audience has
  shown up in the past, falling back to the generic table in
  :mod:`app.services.cadence` where there is not enough evidence yet; this asks
  it, and then makes sure the answer does not land on top of something already
  scheduled.

* **Ordering** a cross-post. When the project names a canonical platform, the
  copies are pushed behind the original by ``syndication_delay_seconds``, for
  the same reason :func:`app.services.publishing_service.queue` does it: a copy
  published before the original has a URL cannot carry a canonical link to it.

Everything here is UTC. The browser renders local time; the database never sees
one.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import cadence, learned_cadence, velocity


class ScheduleError(ValueError):
    """The requested time cannot be honoured. The message is user-facing."""


def normalize(when: datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Validate and canonicalise a requested publish time.

    ``None`` passes through — it means "as soon as a worker picks it up", which
    is a legitimate answer and not a missing one.

    A naive datetime is interpreted as UTC rather than rejected: clients that
    build a timestamp with ``new Date().toISOString()`` always send an offset,
    but the ones that hand-assemble ``"2026-08-04T09:00"`` are common enough
    that refusing them would be pedantry.

    An *aware* datetime is converted to UTC rather than kept in the offset it
    arrived in. :func:`app.models.mixins.as_aware` only labels a naive value; it
    leaves ``2026-09-01T09:00+05:30`` alone, and this function is where the
    module docstring's "everything here is UTC" was supposed to become true. It
    was not, and the consequences run in both directions:

    * the value is stored. ``DateTime(timezone=True)`` is a real ``timestamptz``
      on PostgreSQL, which normalises on the way in, but SQLite's DATETIME
      writes the wall clock and drops the offset — so the whole test suite
      records ``09:00+05:30`` and reads back ``09:00Z``, five and a half hours
      from the instant the client asked for, and every scheduling test written
      against an offset agrees with the bug.
    * the value goes back out. ``scheduled_for`` is echoed in the publication
      and content responses beside ``created_at`` and ``published_at``, which
      are UTC by construction, and a client differencing the three gets a
      number that is wrong by the offset.

    A DST transition is where an offset genuinely carries information rather
    than being an alternative spelling: ``2026-11-01T01:30-04:00`` and
    ``2026-11-01T01:30-05:00`` are the same wall clock in New York an hour
    apart, and the offset is the only thing separating them. Converting keeps
    them apart; dropping it collapses both onto the first.
    """
    if when is None:
        return None

    moment = now or utcnow()
    aware = as_aware(when).astimezone(UTC)

    grace = timedelta(seconds=settings.schedule_past_grace_seconds)
    if aware < moment - grace:
        raise ScheduleError(
            f"{aware.isoformat()} is in the past. Leave the time empty to "
            "publish now, or pick a future one."
        )

    horizon = timedelta(days=settings.schedule_max_horizon_days)
    if aware > moment + horizon:
        raise ScheduleError(
            f"{aware.isoformat()} is more than "
            f"{settings.schedule_max_horizon_days} days out — check the year."
        )
    return aware


def taken_slots(db: Session, user_id: int, *, after: datetime | None = None) -> list[datetime]:
    """Times this user already has something going out.

    Feeds the spacing rule in :func:`app.services.cadence.next_slot`, so
    "schedule three pieces optimally" spreads them across three weeks instead
    of stacking them on the same Tuesday morning.
    """
    floor = after or utcnow()
    rows = db.scalars(
        select(Publication.scheduled_for)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user_id,
            Publication.scheduled_for.is_not(None),
            Publication.scheduled_for >= floor,
            Publication.status.in_(
                [PublicationStatus.PENDING, PublicationStatus.SCHEDULED]
            ),
        )
    )
    return sorted(as_aware(row) for row in rows if row is not None)


@dataclass(frozen=True)
class Slot:
    """One platform's proposed publish time, and why it was proposed."""

    platform: Platform
    when: datetime
    rationale: str

    def as_dict(self) -> dict:
        """The wire shape of one suggested slot, reason included."""
        return {
            "platform": self.platform.value,
            "when": self.when,
            "rationale": self.rationale,
        }


def optimal_slots(
    db: Session,
    user_id: int,
    # ``Sequence``, not ``list``: callers hold a ``list[Platform]`` (the router
    # builds one from the request), and ``list`` is invariant, so the narrower
    # element type is not assignable to ``list[Platform | str]``. Nothing here
    # mutates the argument, so the covariant read-only type is both accurate and
    # what lets those callers pass what they already have.
    platforms: Sequence[Platform | str],
    *,
    after: datetime | None = None,
    canonical: Platform | None = None,
) -> list[Slot]:
    """A publish time per platform, spaced apart and ordered behind the original.

    The canonical platform (when the project names one and it is in this batch)
    is placed first and everything else is pushed at least
    ``syndication_delay_seconds`` behind it — the same rule
    :func:`app.services.publishing_service.queue` applies to an immediate
    cross-post, for the same reason.

    Returns chronological order, which is also dispatch order.
    """
    floor = after or utcnow()
    wanted = [p if isinstance(p, Platform) else Platform(p) for p in platforms]
    # Deduplicate while keeping the caller's order stable.
    ordered = list(dict.fromkeys(wanted))
    if canonical in ordered:
        ordered.remove(canonical)
        ordered.insert(0, canonical)

    occupied = taken_slots(db, user_id, after=floor)
    chosen: list[Slot] = []
    syndication_floor: datetime | None = None
    # One pass over the metric series for the whole batch, shared across every
    # platform below — the learned hours come from the same curves each time,
    # and only ever ask about the first window, so the read is bounded to it.
    known = (
        velocity.curves(
            db, user_id, within_hours=float(settings.velocity_early_window_hours)
        )
        if settings.learned_cadence_enabled
        else []
    )

    for platform in ordered:
        start = floor
        if syndication_floor is not None:
            start = max(start, syndication_floor)
        # The user's own results where there are enough of them, the table
        # where there are not. Which one answered is in the rationale, so a
        # suggested time is never unexplained.
        learned = learned_cadence.learn(db, user_id, platform, known=known)
        when = cadence.next_slot(
            platform, after=start, taken=occupied, using=learned.cadence
        )
        occupied.append(when)
        chosen.append(
            Slot(platform=platform, when=when, rationale=learned.cadence.rationale)
        )
        if canonical is not None and platform == canonical:
            # Everything after this is a copy and must not overtake it.
            syndication_floor = when + timedelta(
                seconds=settings.syndication_delay_seconds
            )

    return sorted(chosen, key=lambda slot: slot.when)


__all__ = [
    "ScheduleError",
    "Slot",
    "normalize",
    "optimal_slots",
    "taken_slots",
]
