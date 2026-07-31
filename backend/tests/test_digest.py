"""The weekly performance email.

Two rules this file exists to hold onto:

* **Gains, not totals.** Platform counters are cumulative, so "views this week"
  is a difference between readings. Summing the snapshots inside the window
  would report the same lifetime views once per poll and make every week look
  like a record.
* **An empty week is not mailed.** Weekly mail that is usually noise trains the
  reader to filter it, and then the one that mattered goes unread too.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import digest, mailer
from app.tasks.digest_tasks import send_weekly_digests


def _now() -> datetime:
    return datetime.now(UTC)


def _no_close(session):
    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def close(self):
            pass

    return lambda: NoCloseProxy()


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Retry logic that does not double-post",
        slug="retry-logic",
        body_markdown="word " * 900,
        status=ContentStatus.PUBLISHED,
        created_at=_now() - timedelta(days=40),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def publication(db, piece) -> Publication:
    row = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=_now() - timedelta(days=30),
        external_url="https://dev.to/r2st/retry-logic",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _snapshot(db, publication, *, days_ago, views, clicks=None, reactions=None):
    """One cumulative reading, as a platform would report it."""
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=_now() - timedelta(days=days_ago),
            views=views,
            clicks=clicks,
            reactions=reactions,
        )
    )
    db.commit()


@pytest.fixture
def sent_mail(monkeypatch):
    """Capture outbound mail and pretend SMTP is configured."""
    outbox: list[dict] = []

    def fake_send(*, to, subject, body, html=None):
        outbox.append({"to": to, "subject": subject, "body": body, "html": html})
        return True

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    return outbox


# --------------------------------------------------------------------------- #
# The numbers                                                                 #
# --------------------------------------------------------------------------- #


def test_views_this_week_is_a_difference_not_a_sum(db, user, piece, publication):
    _snapshot(db, publication, days_ago=10, views=1000)
    _snapshot(db, publication, days_ago=5, views=1200)
    _snapshot(db, publication, days_ago=1, views=1500)

    built = digest.build(db, user)
    # 1500 - 1000, not 1200 + 1500.
    assert built.movement.views == 500


def test_a_first_reading_inside_the_window_counts_whole(db, user, piece, publication):
    """Nothing before it means nothing to subtract."""
    _snapshot(db, publication, days_ago=2, views=300)

    assert digest.build(db, user).movement.views == 300


def test_the_week_before_is_the_comparison(db, user, piece, publication):
    _snapshot(db, publication, days_ago=15, views=100)
    _snapshot(db, publication, days_ago=9, views=300)  # previous window: +200
    _snapshot(db, publication, days_ago=2, views=900)  # this window: +600

    movement = digest.build(db, user).movement
    assert movement.views == 600
    assert movement.previous_views == 200
    assert movement.change == 2.0  # tripled


def test_a_first_week_has_no_comparison_rather_than_a_fake_one(
    db, user, piece, publication
):
    _snapshot(db, publication, days_ago=2, views=400)
    assert digest.build(db, user).movement.change is None


def test_clicks_and_engagement_are_differenced_too(db, user, piece, publication):
    _snapshot(db, publication, days_ago=10, views=100, clicks=10, reactions=4)
    _snapshot(db, publication, days_ago=1, views=400, clicks=35, reactions=20)

    movement = digest.build(db, user).movement
    assert movement.clicks == 25
    # Engagement is reactions + comments + clicks + shares, so this is the 16
    # new reactions plus the 25 new clicks.
    assert movement.engagement == 41


def test_a_counter_going_backwards_never_reports_negative_views(
    db, user, piece, publication
):
    _snapshot(db, publication, days_ago=10, views=5000)
    _snapshot(db, publication, days_ago=1, views=800)

    assert digest.build(db, user).movement.views == 0


# --------------------------------------------------------------------------- #
# What the digest contains                                                    #
# --------------------------------------------------------------------------- #


def test_a_quiet_week_with_nothing_at_all_is_empty(db, user):
    built = digest.build(db, user)
    assert built.is_empty is True


def test_a_quiet_week_that_still_earned_views_is_not_empty(
    db, user, piece, publication
):
    """Last month's tutorial finding an audience is what this is for."""
    _snapshot(db, publication, days_ago=8, views=100)
    _snapshot(db, publication, days_ago=1, views=900)

    built = digest.build(db, user)
    assert built.is_empty is False
    assert built.published == []
    assert built.movement.views == 800


def test_something_waiting_on_a_human_is_reason_enough(db, user, project):
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            title="Waiting on you",
            slug="waiting-on-you",
            status=ContentStatus.REVIEW,
        )
    )
    db.commit()

    built = digest.build(db, user)
    assert built.is_empty is False
    assert built.needs_review == 1


def test_this_weeks_publications_are_listed_with_where_they_went(
    db, user, project, piece
):
    for platform in (Platform.DEVTO, Platform.MEDIUM):
        db.add(
            Publication(
                content_id=piece.id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                published_at=_now() - timedelta(days=2),
                external_url=f"https://{platform.value}.example/post",
            )
        )
    db.commit()

    built = digest.build(db, user)
    assert len(built.published) == 1  # one piece, two destinations
    assert sorted(built.published[0]["platforms"]) == ["devto", "medium"]
    assert built.published[0]["url"]


def test_something_published_before_the_window_is_not_this_weeks_news(
    db, user, piece, publication
):
    assert as_aware(publication.published_at) < _now() - timedelta(days=7)
    assert digest.build(db, user).published == []


def test_failures_and_the_schedule_make_the_digest(db, user, project, piece):
    db.add_all(
        [
            Publication(
                content_id=piece.id,
                platform=Platform.MEDIUM,
                status=PublicationStatus.FAILED,
                error="Medium rejected the credentials (401)",
            ),
            Publication(
                content_id=piece.id,
                platform=Platform.MASTODON,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=_now() + timedelta(days=1),
            ),
        ]
    )
    db.commit()

    built = digest.build(db, user)
    assert built.failed[0]["platform"] == "medium"
    assert "401" in built.failed[0]["error"]
    assert built.upcoming[0]["platform"] == "mastodon"


def test_top_content_ranks_by_what_it_earned_this_week(db, user, project, piece):
    quiet = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Quiet piece",
        slug="quiet-piece",
        status=ContentStatus.PUBLISHED,
    )
    db.add(quiet)
    db.flush()
    loud_pub = Publication(
        content_id=piece.id, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    quiet_pub = Publication(
        content_id=quiet.id, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    db.add_all([loud_pub, quiet_pub])
    db.commit()

    _snapshot(db, loud_pub, days_ago=1, views=5000)
    _snapshot(db, quiet_pub, days_ago=1, views=30)

    top = digest.build(db, user).top
    assert [row["title"] for row in top] == [piece.title, "Quiet piece"]


def test_one_users_week_never_includes_anothers(db, user, piece, publication):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.flush()
    other_project = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(other_project)
    db.flush()
    theirs = Content(
        project_id=other_project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Their post",
        slug="their-post",
        status=ContentStatus.PUBLISHED,
    )
    db.add(theirs)
    db.flush()
    their_pub = Publication(
        content_id=theirs.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=_now() - timedelta(days=1),
    )
    db.add(their_pub)
    db.commit()
    _snapshot(db, their_pub, days_ago=1, views=99_999)

    assert digest.build(db, user).movement.views == 0
    assert digest.build(db, stranger).movement.views == 99_999


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def test_both_renderings_carry_the_numbers_and_the_links(db, user, piece):
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=_now() - timedelta(days=1),
            external_url="https://dev.to/r2st/retry-logic",
        )
    )
    db.commit()
    built = digest.build(db, user)

    text = digest.render_text(built)
    html = digest.render_html(built)

    assert piece.title in text
    assert "https://dev.to/r2st/retry-logic" in text
    assert piece.title in html
    assert 'href="https://dev.to/r2st/retry-logic"' in html


def test_html_escapes_a_title_rather_than_rendering_it(db, user, project):
    nasty = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="<script>alert('x')</script> & friends",
        slug="nasty",
        status=ContentStatus.PUBLISHED,
    )
    db.add(nasty)
    db.flush()
    db.add(
        Publication(
            content_id=nasty.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=_now() - timedelta(days=1),
        )
    )
    db.commit()

    html = digest.render_html(digest.build(db, user))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_subject_says_what_happened(db, user, piece, publication):
    _snapshot(db, publication, days_ago=6, views=0)
    _snapshot(db, publication, days_ago=1, views=1200)

    assert "1,200 views" in digest.build(db, user).subject


# --------------------------------------------------------------------------- #
# Sending                                                                     #
# --------------------------------------------------------------------------- #


def test_send_mails_a_week_with_something_in_it(db, user, piece, publication, sent_mail):
    _snapshot(db, publication, days_ago=6, views=10)
    _snapshot(db, publication, days_ago=1, views=910)

    assert digest.send(db, user) is True
    assert len(sent_mail) == 1
    assert sent_mail[0]["to"] == user.email
    assert sent_mail[0]["html"]
    # The text part is always present — an HTML-only email is unreadable to
    # anything that cannot render it.
    assert sent_mail[0]["body"]


def test_send_stays_quiet_about_an_empty_week(db, user, sent_mail):
    assert digest.send(db, user) is False
    assert sent_mail == []


def test_the_sweep_skips_users_who_opted_out(
    db, user, piece, publication, sent_mail, monkeypatch
):
    _snapshot(db, publication, days_ago=1, views=500)
    user.weekly_digest_enabled = False
    db.commit()

    monkeypatch.setattr("app.tasks.digest_tasks.SessionLocal", _no_close(db))
    assert send_weekly_digests() == {"considered": 0, "sent": 0}
    assert sent_mail == []


def test_the_sweep_mails_subscribers(
    db, user, piece, publication, sent_mail, monkeypatch
):
    _snapshot(db, publication, days_ago=1, views=500)

    monkeypatch.setattr("app.tasks.digest_tasks.SessionLocal", _no_close(db))
    assert send_weekly_digests() == {"considered": 1, "sent": 1}
    assert len(sent_mail) == 1


def test_the_sweep_does_not_dump_a_digest_into_the_log_every_monday(
    db, user, piece, publication, monkeypatch
):
    """The mailer's log fallback is right for a reset link, wrong for this."""
    _snapshot(db, publication, days_ago=1, views=500)
    monkeypatch.setattr(mailer, "configured", lambda: False)
    monkeypatch.setattr("app.tasks.digest_tasks.SessionLocal", _no_close(db))

    result = send_weekly_digests()
    assert result["sent"] == 0
    assert result["skipped"] == "smtp-not-configured"


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def test_preview_endpoint_sends_nothing(client, auth, db, piece, publication, sent_mail):
    _snapshot(db, publication, days_ago=1, views=700)

    resp = client.get("/api/v1/analytics/digest", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["movement"]["views"] == 700
    assert body["is_empty"] is False
    assert body["subject"]
    assert sent_mail == []


def test_send_endpoint_mails_it(client, auth, db, piece, publication, sent_mail):
    _snapshot(db, publication, days_ago=1, views=700)

    resp = client.post("/api/v1/analytics/digest/send", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent"] is True
    assert len(sent_mail) == 1


def test_send_endpoint_explains_an_empty_week(client, auth, sent_mail):
    resp = client.post("/api/v1/analytics/digest/send", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["sent"] is False
    assert "Nothing happened" in resp.json()["reason"]


def test_send_endpoint_explains_unconfigured_smtp(
    client, auth, db, piece, publication, monkeypatch
):
    _snapshot(db, publication, days_ago=1, views=700)
    monkeypatch.setattr(mailer, "configured", lambda: False)

    resp = client.post("/api/v1/analytics/digest/send", headers=auth)
    assert resp.json()["sent"] is False
    assert "SMTP" in resp.json()["reason"]


def test_digest_endpoints_need_authentication(client):
    assert client.get("/api/v1/analytics/digest").status_code == 401
    assert client.post("/api/v1/analytics/digest/send").status_code == 401


def test_the_preference_can_be_turned_off_and_back_on(client, auth):
    assert client.get("/api/v1/auth/me", headers=auth).json()["weekly_digest_enabled"]

    resp = client.patch(
        "/api/v1/auth/me", headers=auth, json={"weekly_digest_enabled": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["weekly_digest_enabled"] is False

    resp = client.patch(
        "/api/v1/auth/me", headers=auth, json={"weekly_digest_enabled": True}
    )
    assert resp.json()["weekly_digest_enabled"] is True
