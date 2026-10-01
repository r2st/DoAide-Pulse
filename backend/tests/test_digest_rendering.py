"""The parts of the weekly email that only appear when the week went badly.

``test_digest.py`` covers the numbers and the send decision. What it does not
reach is the bottom half of both renderers — the review backlog, the publishes
that failed, and what is queued next. Those sections are the reason a user opens
the email on a week with no wins, so "renders at all" is worth pinning, and so is
"escapes what came off a platform's error message" in the HTML.

Both renderers are driven from a hand-built :class:`Digest` rather than from the
database: the query side is already covered, and building the object directly is
the only way to pin a section against a value the fixtures cannot easily produce.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import digest as digest_service
from app.services.digest import Digest, Movement

SINCE = datetime(2026, 8, 3, tzinfo=UTC)
UNTIL = datetime(2026, 8, 10, tzinfo=UTC)


def _digest(**overrides) -> Digest:
    base = {
        "user_id": 1,
        "email": "dev@example.com",
        "name": "Dev",
        "since": SINCE,
        "until": UNTIL,
        "movement": Movement(),
    }
    return Digest(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# Subject lines                                                                #
# --------------------------------------------------------------------------- #


def test_the_subject_leads_with_what_was_published():
    d = _digest(
        published=[{"title": "A", "platforms": ["devto"], "url": ""}],
        movement=Movement(views=1234),
    )

    assert d.subject == "Pulse: 1 published, 1,234 views this week"


def test_a_week_with_only_reads_says_so():
    """Nothing shipped, but last month's tutorial found an audience."""
    d = _digest(movement=Movement(views=9000))

    assert d.subject == "Pulse: 9,000 views this week"


def test_a_week_with_neither_asks_for_attention():
    d = _digest(needs_review=3)

    assert d.subject == "Pulse: this week needs you"


# --------------------------------------------------------------------------- #
# is_empty                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    [
        {"published": [{"title": "A", "platforms": [], "url": ""}]},
        {"failed": [{"title": "A", "platform": "devto", "error": "x", "content_id": 1}]},
        {"upcoming": [{"title": "A", "platform": "devto", "scheduled_for": None,
                       "content_id": 1}]},
        {"needs_review": 1},
        {"movement": Movement(views=1)},
        {"movement": Movement(engagement=1)},
    ],
)
def test_any_one_of_these_is_reason_enough_to_send(field):
    assert _digest(**field).is_empty is False


def test_an_alert_alone_is_not_reason_enough_to_send():
    """An alert is about something published weeks ago.

    On its own it does not justify putting mail in an otherwise silent week's
    inbox — weekly mail that is usually noise trains the reader to filter it.
    """
    d = _digest(attention=[{"title": "A", "message": "down on its own normal"}])

    assert d.is_empty is True


def test_a_week_with_nothing_at_all_is_empty():
    assert _digest().is_empty is True


# --------------------------------------------------------------------------- #
# The plain-text body                                                          #
# --------------------------------------------------------------------------- #


def test_the_review_backlog_is_stated_in_the_text_body():
    body = digest_service.render_text(_digest(needs_review=4))

    assert "4 draft(s) waiting for your review." in body


def test_each_failed_publish_names_the_platform_and_the_error():
    body = digest_service.render_text(
        _digest(
            failed=[
                {
                    "content_id": 1,
                    "title": "Retry logic",
                    "platform": "devto",
                    "error": "422 Unprocessable Entity",
                },
                {
                    "content_id": 2,
                    "title": "Second piece",
                    "platform": "hashnode",
                    "error": "token rejected",
                },
            ]
        )
    )

    assert "Failed on devto: Retry logic — 422 Unprocessable Entity" in body
    assert "Failed on hashnode: Second piece — token rejected" in body


def test_what_is_queued_next_carries_its_time():
    body = digest_service.render_text(
        _digest(
            upcoming=[
                {
                    "content_id": 1,
                    "title": "Scheduled piece",
                    "platform": "devto",
                    "scheduled_for": UNTIL + timedelta(days=1, hours=9),
                }
            ]
        )
    )

    assert "Going out next:" in body
    assert "Scheduled piece → devto on 11 Aug 09:00 UTC" in body


def test_a_queued_item_with_no_time_still_renders():
    """``scheduled_for`` is nullable; a missing one must not format as ``None``."""
    body = digest_service.render_text(
        _digest(
            upcoming=[
                {
                    "content_id": 1,
                    "title": "Undated piece",
                    "platform": "devto",
                    "scheduled_for": None,
                }
            ]
        )
    )

    assert "Undated piece → devto" in body
    assert "None" not in body


def test_the_worth_a_look_section_renders_each_alert():
    body = digest_service.render_text(
        _digest(
            attention=[
                {"title": "Old tutorial", "message": "40% below its own normal"}
            ]
        )
    )

    assert "Worth a look:" in body
    assert "Old tutorial — 40% below its own normal" in body


def test_a_week_with_nothing_still_renders_a_body_rather_than_crashing():
    """The renderer runs before the send decision in some paths."""
    body = digest_service.render_text(_digest())

    assert "Hello Dev," in body
    assert body.rstrip().endswith(digest_service.settings.frontend_url)


# --------------------------------------------------------------------------- #
# The HTML body                                                                #
# --------------------------------------------------------------------------- #


def test_the_html_states_the_review_backlog():
    html = digest_service.render_html(_digest(needs_review=2))

    assert "<strong>2</strong> draft(s) waiting for your review." in html


def test_the_html_lists_what_did_not_go_out():
    html = digest_service.render_html(
        _digest(
            failed=[
                {
                    "content_id": 1,
                    "title": "Retry logic",
                    "platform": "devto",
                    "error": "422 Unprocessable Entity",
                }
            ]
        )
    )

    assert "Did not go out" in html
    assert "Retry logic on devto — 422 Unprocessable Entity" in html


def test_a_platform_error_containing_markup_is_escaped_not_rendered():
    """The error text comes off a third-party API and lands in an HTML email."""
    html = digest_service.render_html(
        _digest(
            failed=[
                {
                    "content_id": 1,
                    "title": "<script>alert(1)</script>",
                    "platform": "devto",
                    "error": '<img src=x onerror="alert(2)">',
                }
            ]
        )
    )

    # Nothing survives as a tag; both are present, both as text.
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in html


def test_the_html_lists_what_is_going_out_next():
    html = digest_service.render_html(
        _digest(
            upcoming=[
                {
                    "content_id": 1,
                    "title": "Scheduled piece",
                    "platform": "devto",
                    "scheduled_for": UNTIL + timedelta(days=1, hours=9),
                }
            ]
        )
    )

    assert "Going out next" in html
    assert "Scheduled piece" in html
    assert "11 Aug 09:00 UTC" in html


def test_an_undated_queued_item_renders_in_html_too():
    html = digest_service.render_html(
        _digest(
            upcoming=[
                {
                    "content_id": 1,
                    "title": "Undated piece",
                    "platform": "devto",
                    "scheduled_for": None,
                }
            ]
        )
    )

    assert "Undated piece" in html
    assert "None" not in html


def test_the_html_worth_a_look_section_escapes_its_rows():
    html = digest_service.render_html(
        _digest(attention=[{"title": "A & B", "message": "down <30%>"}])
    )

    assert "Worth a look" in html
    assert "A &amp; B" in html
    assert "<30%>" not in html


def test_every_section_can_appear_at_once_without_colliding():
    html = digest_service.render_html(
        _digest(
            movement=Movement(views=500, clicks=20, engagement=9, previous_views=250),
            published=[{"title": "Shipped", "platforms": ["devto"], "url": "https://x"}],
            top=[{"content_id": 1, "title": "Shipped", "views": 400}],
            needs_review=1,
            failed=[{"content_id": 2, "title": "Broke", "platform": "medium",
                     "error": "500"}],
            upcoming=[{"content_id": 3, "title": "Next", "platform": "devto",
                       "scheduled_for": UNTIL}],
            attention=[{"title": "Old", "message": "quiet"}],
        )
    )

    for expected in ("Shipped", "Broke", "Next", "Old", "draft(s) waiting"):
        assert expected in html
    assert html.count("<div") == html.count("</div>")


def test_as_dict_carries_every_section_the_api_shows():
    d = _digest(
        needs_review=1,
        failed=[{"content_id": 1, "title": "A", "platform": "devto", "error": "e"}],
        upcoming=[{"content_id": 2, "title": "B", "platform": "devto",
                   "scheduled_for": None}],
        attention=[{"title": "C", "message": "m"}],
    )

    body = d.as_dict()

    assert body["needs_review"] == 1
    assert body["failed"][0]["title"] == "A"
    assert body["upcoming"][0]["title"] == "B"
    assert body["attention"][0]["title"] == "C"
    assert body["subject"] == "Pulse: this week needs you"
    assert body["is_empty"] is False
