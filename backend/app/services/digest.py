"""The weekly performance email.

One message a week answering "was any of that worth it?" — what went out, what
it earned, and the two or three things waiting on a human. Deliberately not a
dashboard in an email: everything here is either a number that changed or a
thing to do, because a digest nobody reads is worse than no digest.

Two decisions shape the numbers:

* **Gains, not totals.** Platform counters are cumulative, so "views this week"
  is the difference between the latest snapshot and the last one taken before
  the window opened — never a sum of the snapshots inside it, which would count
  the same lifetime views once per poll. Same reasoning as
  :mod:`app.services.headlines`.
* **Nothing happened is a reason not to send.** A week with no publishing, no
  movement and nothing waiting produces an empty digest, and an empty digest is
  not mailed. Weekly mail that is usually noise trains the reader to filter it,
  and then the one that mattered goes unread too.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content, ContentStatus
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.services import alerts, mailer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Movement:
    """One metric over the window, against the window before it."""

    views: int = 0
    clicks: int = 0
    engagement: int = 0
    previous_views: int = 0

    @property
    def change(self) -> float | None:
        """Views this window against the last, as a fraction. ``None`` if new.

        A first week has nothing to compare against, and rendering that as
        +100% would be an invention.
        """
        if not self.previous_views:
            return None
        return round((self.views - self.previous_views) / self.previous_views, 3)


@dataclass
class Digest:
    """Everything one weekly email says."""

    user_id: int
    email: str
    name: str
    since: datetime
    until: datetime
    movement: Movement = field(default_factory=Movement)
    #: Pieces that went live during the window.
    published: list[dict] = field(default_factory=list)
    #: Best performers over the window, by views gained.
    top: list[dict] = field(default_factory=list)
    #: Things waiting on a human.
    needs_review: int = 0
    failed: list[dict] = field(default_factory=list)
    upcoming: list[dict] = field(default_factory=list)
    #: Posts doing worse than this user's own normal — see
    #: :mod:`app.services.alerts`. Not counted by :attr:`is_empty`: an alert is
    #: about something published weeks ago, and it alone is not news enough to
    #: put an email in an otherwise silent week's inbox.
    attention: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing worth an email.

        Movement counts even with nothing published: a quiet week where last
        month's tutorial found an audience is exactly what the digest is for.
        """
        return not (
            self.published
            or self.failed
            or self.upcoming
            or self.needs_review
            or self.movement.views
            or self.movement.engagement
        )

    @property
    def subject(self) -> str:
        if self.published:
            return (
                f"Herald: {len(self.published)} published, "
                f"{self.movement.views:,} views this week"
            )
        if self.movement.views:
            return f"Herald: {self.movement.views:,} views this week"
        return "Herald: this week needs you"

    def as_dict(self) -> dict:
        return {
            "since": self.since,
            "until": self.until,
            "subject": self.subject,
            "is_empty": self.is_empty,
            "movement": {
                "views": self.movement.views,
                "clicks": self.movement.clicks,
                "engagement": self.movement.engagement,
                "previous_views": self.movement.previous_views,
                "change": self.movement.change,
            },
            "published": self.published,
            "top": self.top,
            "needs_review": self.needs_review,
            "failed": self.failed,
            "upcoming": self.upcoming,
            "attention": self.attention,
        }


def _gains(
    db: Session, user_id: int, *, since: datetime, until: datetime
) -> tuple[Movement, dict[int, int]]:
    """Views/clicks/engagement gained in the window, overall and per content.

    Per publication, the gain is the last reading inside the window minus the
    last reading before it — the counters are cumulative, so a difference is
    the only honest reading of "this week". A publication first seen inside the
    window counts its whole first reading, since it had nothing before.
    """
    span = until - since
    earlier_start = since - span

    rows = db.execute(
        select(ContentMetric, Publication.content_id)
        .join(Publication, Publication.id == ContentMetric.publication_id)
        .join(Content, Content.id == Publication.content_id)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id, ContentMetric.captured_at < until)
        .order_by(ContentMetric.publication_id, ContentMetric.captured_at)
    ).all()

    # Per publication, the last reading in each of three periods: before the
    # comparison window, inside it, and inside the window being reported. Two
    # differences fall out of that — this week, and the week before it.
    prior: dict[int, ContentMetric] = {}
    baseline: dict[int, ContentMetric] = {}
    latest: dict[int, ContentMetric] = {}
    content_of: dict[int, int] = {}
    for metric, content_id in rows:
        content_of[metric.publication_id] = content_id
        captured = as_aware(metric.captured_at)
        if captured < earlier_start:
            prior[metric.publication_id] = metric
        elif captured < since:
            baseline[metric.publication_id] = metric
        else:
            latest[metric.publication_id] = metric

    def _delta(end: ContentMetric | None, start: ContentMetric | None, field_: str) -> int:
        if end is None:
            return 0
        after = getattr(end, field_)
        if field_ == "engagement":
            before = start.engagement if start else 0
        else:
            before = (getattr(start, field_) or 0) if start else 0
            after = after or before
        return max(0, (after or 0) - before)

    views = clicks = engagement = previous = 0
    per_content: dict[int, int] = {}
    for publication_id in set(latest) | set(baseline):
        # A publication first seen inside the window has no earlier reading, so
        # its whole first count is this window's gain — it had nothing before.
        start = baseline.get(publication_id) or prior.get(publication_id)
        end = latest.get(publication_id)
        gained = _delta(end, start, "views")
        views += gained
        clicks += _delta(end, start, "clicks")
        engagement += _delta(end, start, "engagement")
        previous += _delta(
            baseline.get(publication_id), prior.get(publication_id), "views"
        )
        if gained:
            content_id = content_of[publication_id]
            per_content[content_id] = per_content.get(content_id, 0) + gained

    return Movement(
        views=views, clicks=clicks, engagement=engagement, previous_views=previous
    ), per_content


def build(
    db: Session, user: User, *, days: int | None = None, now: datetime | None = None
) -> Digest:
    """Assemble one user's digest for the trailing window."""
    until = now or utcnow()
    window = days if days is not None else settings.digest_window_days
    since = until - timedelta(days=window)

    movement, per_content = _gains(db, user.id, since=since, until=until)

    published_rows = db.execute(
        select(Content, Publication)
        .join(Publication, Publication.content_id == Content.id)
        .join(Project, Project.id == Content.project_id)
        .where(
            Project.user_id == user.id,
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.published_at.is_not(None),
            Publication.published_at >= since,
            Publication.published_at < until,
        )
        .order_by(Publication.published_at.desc())
    ).all()

    published: dict[int, dict] = {}
    for content, publication in published_rows:
        entry = published.setdefault(
            content.id,
            {
                "content_id": content.id,
                "title": content.title,
                "read_minutes": content.read_minutes,
                "platforms": [],
                "url": None,
                "views": per_content.get(content.id, 0),
            },
        )
        entry["platforms"].append(publication.platform.value)
        if entry["url"] is None and publication.external_url:
            entry["url"] = publication.external_url

    titles = {
        c.id: c
        for c in db.scalars(
            select(Content).where(Content.id.in_(list(per_content) or [0]))
        )
    }
    top = sorted(
        (
            {
                "content_id": content_id,
                "title": titles[content_id].title,
                "views": gained,
            }
            for content_id, gained in per_content.items()
            if gained > 0 and content_id in titles
        ),
        key=lambda row: row["views"],
        reverse=True,
    )[:5]

    review_count = db.scalar(
        select(func.count(Content.id))
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user.id, Content.status == ContentStatus.REVIEW)
    ) or 0

    failed = [
        {
            "content_id": p.content_id,
            "title": p.content.title,
            "platform": p.platform.value,
            "error": (p.error or "")[:200],
        }
        for p in db.scalars(
            select(Publication)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(
                Project.user_id == user.id,
                Publication.status == PublicationStatus.FAILED,
                Publication.updated_at >= since,
            )
            .order_by(Publication.updated_at.desc())
            .limit(5)
        )
    ]

    upcoming = [
        {
            "content_id": p.content_id,
            "title": p.content.title,
            "platform": p.platform.value,
            "scheduled_for": as_aware(p.scheduled_for) if p.scheduled_for else None,
        }
        for p in db.scalars(
            select(Publication)
            .join(Content, Content.id == Publication.content_id)
            .join(Project, Project.id == Content.project_id)
            .where(
                Project.user_id == user.id,
                Publication.status == PublicationStatus.SCHEDULED,
                Publication.scheduled_for.is_not(None),
            )
            .order_by(Publication.scheduled_for)
            .limit(5)
        )
    ]

    return Digest(
        user_id=user.id,
        email=user.email,
        name=(user.full_name or "").strip() or user.email.split("@")[0],
        since=since,
        until=until,
        movement=movement,
        published=list(published.values()),
        top=top,
        needs_review=review_count,
        failed=failed,
        upcoming=upcoming,
        # Three, not ten: the email is a prompt to open the dashboard, and a
        # list long enough to scroll is one that gets archived unread.
        attention=[a.as_dict() for a in alerts.build(db, user.id, limit=3, now=until)],
    )


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def _change_line(movement: Movement) -> str:
    change = movement.change
    if change is None:
        return ""
    direction = "up" if change >= 0 else "down"
    return f" ({direction} {abs(round(change * 100))}% on the week before)"


def render_text(digest: Digest) -> str:
    """The plain-text body. Also the fallback when SMTP is not configured."""
    lines = [
        f"Hello {digest.name},",
        "",
        f"Herald, {digest.since:%d %b} to {digest.until:%d %b}.",
        "",
        f"  Views       {digest.movement.views:,}{_change_line(digest.movement)}",
        f"  Clicks      {digest.movement.clicks:,}",
        f"  Engagement  {digest.movement.engagement:,}",
        "",
    ]

    if digest.published:
        lines.append(f"Published this week ({len(digest.published)}):")
        for row in digest.published:
            where = ", ".join(sorted(row["platforms"]))
            lines.append(f"  - {row['title']} — {where}")
            if row["url"]:
                lines.append(f"    {row['url']}")
        lines.append("")

    if digest.top:
        lines.append("Most read this week:")
        for row in digest.top:
            lines.append(f"  - {row['title']} — {row['views']:,} views")
        lines.append("")

    if digest.attention:
        lines.append("Worth a look:")
        for row in digest.attention:
            lines.append(f"  - {row['title']} — {row['message']}")
        lines.append("")

    if digest.needs_review:
        lines.append(f"{digest.needs_review} draft(s) waiting for your review.")
    for row in digest.failed:
        lines.append(f"Failed on {row['platform']}: {row['title']} — {row['error']}")
    if digest.upcoming:
        lines.append("")
        lines.append("Going out next:")
        for row in digest.upcoming:
            when = row["scheduled_for"]
            lines.append(
                f"  - {row['title']} → {row['platform']}"
                + (f" on {when:%d %b %H:%M} UTC" if when else "")
            )

    lines += ["", "— Herald", settings.frontend_url]
    return "\n".join(lines)


def render_html(digest: Digest) -> str:
    """The HTML body.

    Inline styles and a table-free layout on purpose: this is read in Gmail and
    Apple Mail, neither of which can be relied on for a stylesheet, and a
    digest that arrives unreadable is worse than one that arrives plain.
    """
    def esc(value) -> str:
        return escape(str(value), quote=True)

    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:600px;margin:0 auto;color:#1c1c1e;line-height:1.5">',
        f"<p>Hello {esc(digest.name)},</p>",
        f"<p style=\"color:#6b7280\">Herald, {digest.since:%d %b} to "
        f"{digest.until:%d %b}.</p>",
        '<p style="font-size:15px">'
        f"<strong style=\"font-size:22px\">{digest.movement.views:,}</strong> views"
        f"{esc(_change_line(digest.movement))}<br>"
        f"<strong>{digest.movement.clicks:,}</strong> clicks &middot; "
        f"<strong>{digest.movement.engagement:,}</strong> interactions</p>",
    ]

    if digest.published:
        parts.append(f"<h3>Published this week ({len(digest.published)})</h3><ul>")
        for row in digest.published:
            where = esc(", ".join(sorted(row["platforms"])))
            title = (
                f'<a href="{esc(row["url"])}">{esc(row["title"])}</a>'
                if row["url"]
                else esc(row["title"])
            )
            parts.append(f"<li>{title} — <em>{where}</em></li>")
        parts.append("</ul>")

    if digest.top:
        parts.append("<h3>Most read this week</h3><ul>")
        for row in digest.top:
            parts.append(f"<li>{esc(row['title'])} — {row['views']:,} views</li>")
        parts.append("</ul>")

    if digest.attention:
        parts.append("<h3>Worth a look</h3><ul>")
        for row in digest.attention:
            parts.append(
                f"<li>{esc(row['title'])} — {esc(row['message'])}</li>"
            )
        parts.append("</ul>")

    if digest.needs_review:
        parts.append(
            f'<p><strong>{digest.needs_review}</strong> draft(s) waiting for your '
            "review.</p>"
        )

    if digest.failed:
        parts.append("<h3>Did not go out</h3><ul>")
        for row in digest.failed:
            parts.append(
                f"<li>{esc(row['title'])} on {esc(row['platform'])} — "
                f"{esc(row['error'])}</li>"
            )
        parts.append("</ul>")

    if digest.upcoming:
        parts.append("<h3>Going out next</h3><ul>")
        for row in digest.upcoming:
            when = row["scheduled_for"]
            suffix = f" on {when:%d %b %H:%M} UTC" if when else ""
            parts.append(
                f"<li>{esc(row['title'])} &rarr; {esc(row['platform'])}{esc(suffix)}</li>"
            )
        parts.append("</ul>")

    parts.append(
        f'<p style="color:#6b7280;font-size:13px">— Herald &middot; '
        f'<a href="{esc(settings.frontend_url)}">{esc(settings.frontend_url)}</a></p>'
        "</div>"
    )
    return "".join(parts)


def send(db: Session, user: User, *, now: datetime | None = None) -> bool:
    """Build and send one user's digest. Returns whether anything was sent.

    An empty week is not sent and is not an error — see the module docstring.
    """
    digest = build(db, user, now=now)
    if digest.is_empty:
        logger.info("digest for %s skipped: nothing happened this week", user.email)
        return False

    return mailer.send(
        to=user.email,
        subject=digest.subject,
        body=render_text(digest),
        html=render_html(digest),
    )


__all__ = ["Digest", "Movement", "build", "render_html", "render_text", "send"]
