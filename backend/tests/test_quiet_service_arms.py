"""The arms that only run when something is absent.

Every branch here is a "the caller left this out" or "the platform reported
nothing" path: no ideas on a seed spec, no description on a card, no detail on
a trigger event, a snapshot with a ``NULL`` views column, a reset for an account
that has since been deactivated. They are the branches a happy-path test never
reaches, and — because absence is what production data is full of — the ones
most likely to be wrong without anyone noticing.
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.trigger import Trigger, TriggerKind
from app.models.user import User
from app.services import (
    analytics_service,
    formats,
    headlines,
    mailer,
    password_reset,
    preview_links,
    social_cards,
    triggers,
)


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        title="A piece",
        slug="a-piece",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown="Body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(db, content, *, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED):
    row = Publication(
        content_id=content.id,
        platform=platform,
        status=status,
        published_at=(
            utcnow() - timedelta(days=3) if status == PublicationStatus.PUBLISHED else None
        ),
        external_url="https://dev.to/x/a-piece",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- Analytics: the columns a platform declined to fill --------------------- #


def test_totals_can_be_narrowed_to_one_project(db, user, project, content):
    """The filter arm — the dashboard asks for everything, a project page does not."""
    other = Project(user_id=user.id, name="Other", slug="other")
    db.add(other)
    db.commit()
    db.add(
        Content(
            project_id=other.id,
            title="Elsewhere",
            slug="elsewhere",
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.PUBLISHED,
            body_markdown="Body.",
        )
    )
    db.commit()

    everything = analytics_service.totals(db, user.id)
    narrowed = analytics_service.totals(db, user.id, project_id=project.id)

    assert everything.content_count == 2
    assert narrowed.content_count == 1


def test_a_snapshot_with_no_views_is_not_counted_as_a_zero_view_sample(db, content):
    """``with_views`` is the denominator of ``avg_views``.

    A platform that reported engagement but no view count must not join that
    sample: counting it would divide the same views across one more piece and
    report every content type as doing worse than it is.
    """
    publication = _publication(db, content)
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=utcnow() - timedelta(hours=1),
            views=None,
            reactions=4,
        )
    )
    db.commit()

    (row,) = [
        r
        for r in analytics_service.by_content_type(db, content.project.user_id)
        if r["publications"]
    ]
    assert row["engagement"] == 4
    assert row["avg_views"] is None


def test_a_platform_that_rejected_every_post_is_counted_as_failed_not_quiet(db, content):
    """"No views" and "never got published" need very different responses."""
    _publication(db, content, platform=Platform.DEVTO)
    _publication(
        db, content, platform=Platform.LINKEDIN, status=PublicationStatus.FAILED
    )

    rows = {r["platform"]: r for r in analytics_service.by_platform(db, content.project.user_id)}

    assert rows["devto"]["published"] == 1
    assert rows["devto"]["failed"] == 0
    assert rows["linkedin"]["failed"] == 1
    assert rows["linkedin"]["published"] == 0


# ---- Headlines: a snapshot from before the first headline ------------------- #


def test_a_snapshot_older_than_every_window_is_dropped_rather_than_credited(db, content):
    """``_window_for`` returning ``None`` is the guard against inventing history.

    The windows start at the content's ``created_at``. A snapshot captured
    before that belongs to no headline — it is a backdated import or a clock
    skew — and crediting it to the first window would hand a headline views
    earned before it existed.
    """
    publication = _publication(db, content)
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=content.created_at - timedelta(days=30),
            views=9_999,
        )
    )
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=utcnow(),
            views=10,
        )
    )
    db.commit()

    windows = headlines.performance(content, db)

    assert len(windows) == 1
    assert windows[0].snapshots == 1
    assert windows[0].views == 10


# ---- Formats: a token that does not fit at all ------------------------------ #


def test_a_single_word_longer_than_the_limit_starts_its_own_post():
    """A URL longer than the whole post budget still leads the thread.

    ``_tokens`` cuts it into limit-sized chunks first, so the splitter is never
    handed a piece it cannot place — the URL opens the first post rather than
    being dropped or overflowing it.
    """
    url = "https://example.com/" + "x" * 400

    parts = formats.split_post(f"{url} and then some trailing words.", limit=100)

    assert "" not in parts
    assert parts[0].startswith("https://example.com/")
    assert all(len(p) <= 100 for p in parts)


def test_text_that_divides_evenly_leaves_nothing_in_hand():
    """Two sentences that each exactly fill a post produce exactly two posts."""
    parts = formats.split_post("A" * 40 + ". " + "B" * 40 + ".", limit=45)

    assert parts == ["A" * 40 + ".", "B" * 40 + "."]
    assert "" not in parts


def test_a_run_of_stops_stays_inside_one_post():
    """``First one....`` is one piece, not a piece plus three empty ones.

    ``_SENTENCE_END`` matches whitespace after a stop, so a run of stops with
    nothing between them is never a split point and the ellipsis travels with
    the sentence it belongs to.
    """
    parts = formats.split_post("First one.... Second one." + " tail" * 60, limit=60)

    assert "" not in parts
    assert parts[0].startswith("First one....")


# ---- Mailer: the HTML alternative ------------------------------------------- #


class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.message = message


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "pulse@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "smtp_from", "Pulse <pulse@example.com>")
    return _FakeSMTP


def test_an_html_body_is_added_as_the_preferred_alternative(smtp):
    """Order is the assertion: the text part has to survive as the fallback.

    The digest is the one mail Pulse sends with both, and a client that cannot
    render HTML — or a user who has switched it off — gets the text part only
    if it is still there underneath.
    """
    assert mailer.send(
        to="reader@example.com",
        subject="Your week",
        body="Plain text version.",
        html="<p>Rich version.</p>",
    )

    message = smtp.instances[0].message
    assert message.is_multipart()
    subtypes = [part.get_content_subtype() for part in message.walk() if not part.is_multipart()]
    assert subtypes == ["plain", "html"]


# ---- Password reset: an account that is no longer usable -------------------- #


def test_a_valid_reset_token_for_a_deactivated_account_is_refused(db, user):
    """The token is good; the account is not.

    Deactivation has to win, or it is reversible by anyone holding a reset
    email that was sent before it — including the person the account was
    deactivated because of.
    """
    raw_token = password_reset.issue(db, user)
    user.is_active = False
    db.commit()

    assert password_reset.consume(db, raw_token, "a-brand-new-password") is None

    db.refresh(user)
    assert user.is_active is False


def test_a_reset_token_whose_user_is_gone_is_refused(db, user):
    """A row can outlive its user — the delete cascades, but not inside one call."""
    raw_token = password_reset.issue(db, user)
    user_id = user.id
    db.delete(user)
    db.commit()

    assert password_reset.consume(db, raw_token, "a-brand-new-password") is None
    assert db.get(User, user_id) is None


# ---- Preview links: revoking twice ------------------------------------------ #


def test_revoking_an_already_revoked_link_keeps_the_first_timestamp(db, content):
    """Idempotent, and the first revocation is the one that counts.

    Overwriting would move the audit trail forward every time somebody clicked
    the button again, so "when did this stop working" would answer with the
    last click rather than the first.
    """
    link, _ = preview_links.issue(db, content)
    preview_links.revoke(db, link)
    first = link.revoked_at
    assert first is not None

    preview_links.revoke(db, link)

    assert link.revoked_at == first


# ---- Social cards: the fields nobody filled in ------------------------------ #


def test_a_published_time_is_carried_into_the_card_when_there_is_one():
    tags = dict(
        social_cards.meta_tags(
            title="A piece",
            url="https://pulse.example.com/a-piece",
            meta_description="What it is about.",
            published_at="2026-08-10T12:00:00+00:00",
        )
    )

    assert tags["article:published_time"] == "2026-08-10T12:00:00+00:00"


def test_no_published_time_leaves_the_tag_off_rather_than_empty():
    """An empty ``article:published_time`` is worse than none: some crawlers
    parse it as the epoch and date the piece to 1970."""
    names = [name for name, _ in social_cards.meta_tags(title="A piece", url="https://x.test/a")]

    assert "article:published_time" not in names


def test_a_card_with_no_title_is_an_error_not_a_warning():
    issues = {(i.level, i.field) for i in social_cards.audit(title="   ")}

    assert ("error", "title") in issues


def test_a_card_with_nothing_to_describe_it_is_flagged():
    """No meta description, no excerpt, no body — nothing to derive one from."""
    issues = {
        (i.level, i.field)
        for i in social_cards.audit(
            title="A piece", cover_image_url="https://cdn.test/card.png"
        )
    }

    assert ("warn", "meta_description") in issues


# ---- Triggers: the schedule that matches, the event with nothing to say ----- #


@pytest.fixture
def schedule_trigger(db, project) -> Trigger:
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.SCHEDULE,
        name="Weekly",
        config={"hour_utc": 9, "every_hours": 168},
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_schedule_trigger_is_due_once_the_hour_matches(db, schedule_trigger):
    """The hour gate falls through when it matches — the window decides next.

    Both arms of the same ``if`` are load-bearing and only one had ever run:
    the wrong hour returns ``False`` outright, the right hour has to carry on
    to the interval check rather than firing on the strength of the hour alone.
    """
    at_nine = datetime(2026, 8, 10, 9, 30, tzinfo=utcnow().tzinfo)

    assert triggers.is_due(schedule_trigger, moment=at_nine) is True
    assert triggers.is_due(schedule_trigger, moment=at_nine.replace(hour=10)) is False


def test_a_matching_hour_still_waits_out_the_interval(db, schedule_trigger):
    """The hour is a gate, not a trigger — firing twice in an hour is a bug."""
    at_nine = datetime(2026, 8, 10, 9, 30, tzinfo=utcnow().tzinfo)
    schedule_trigger.last_fired_at = at_nine - timedelta(hours=2)
    db.commit()

    assert triggers.is_due(schedule_trigger, moment=at_nine) is False


def test_an_event_with_no_detail_reports_without_an_empty_field(db, project, schedule_trigger):
    """``detail`` carries the failure reason, so it is empty on success.

    The column is ``NOT NULL`` with an empty-string default, so the absent case
    is ``""`` rather than ``None`` — and the key has to be left out of the body
    entirely. The UI shows the detail row whenever the key is present, and an
    empty one reads as "something went wrong but we cannot say what".
    """
    signal = triggers.signal_from_schedule(schedule_trigger)
    event = triggers.record(db, schedule_trigger, signal)
    assert event is not None
    event.detail = ""
    db.commit()

    body = triggers._fired(event)

    assert "detail" not in body
    assert body["event_id"] == event.id
    assert body["status"] == event.status.value
