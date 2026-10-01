"""Backoff between publication-level retry attempts.

The inner retry loop in ``publishers.base`` answers blips that resolve in
seconds. This is the outer one, and it used to answer nothing at all: a
retryable failure returned the row to ``pending`` with no ``scheduled_for``,
which ``due_publications`` treats as due right now. The three attempts were
therefore spent at whatever cadence the sweep runs at, and a platform having a
half-hour outage saw all three land inside it. The piece went terminal while the
outage was still going, and the only way back was a hand-retry by somebody with
no reason to be looking.

What is asserted here is mostly *timing*, which usually means a brittle test.
It is kept honest by comparing against the configured settings rather than
against wall-clock constants, and by pinning the properties that actually
matter — the wait grows, it is capped, the budget is unchanged, and the row is
invisible to the sweep until its time.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import as_aware, utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, PublicationStatus
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import PublishError, RateLimited


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Pulse 1.0 is out.",
        meta_description="Pulse 1.0 is out.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user):
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@r2st",
    )
    db.add(row)
    db.commit()
    return row


def _always_fails(message: str = "devto is down"):
    def publish(self, request, credentials):
        raise PublishError(message)

    return publish


@pytest.fixture
def failing(monkeypatch):
    from app.services.publishers.devto import DevToAdapter

    monkeypatch.setattr(DevToAdapter, "publish", _always_fails())


# --------------------------------------------------------------------------- #
# The delay itself                                                             #
# --------------------------------------------------------------------------- #


def test_the_first_retry_waits_the_base_window():
    """``attempts`` is the count already spent, so one failure waits once.

    Off-by-one here would either skip the first wait entirely — the bug this
    replaces — or double it, which halves how many attempts fit in an outage.
    """
    assert publishing_service.retry_defer_seconds(1) == pytest.approx(
        settings.publish_retry_defer_seconds
    )


def test_each_attempt_waits_longer_than_the_last():
    waits = [publishing_service.retry_defer_seconds(n) for n in range(1, 5)]
    assert waits == sorted(waits)
    assert waits[1] == pytest.approx(waits[0] * 2)


def test_the_wait_is_capped():
    """Otherwise the last attempt of a long budget lands days away.

    A post that quietly disappears for a day is indistinguishable from a bug,
    which is the same reasoning behind the rate-limit defer ceiling.
    """
    assert publishing_service.retry_defer_seconds(50) == pytest.approx(
        settings.publish_retry_max_defer_seconds
    )


def test_a_zero_window_means_no_wait(monkeypatch):
    """The opt-out has to actually opt out.

    Zero restores the old behaviour — retry on the next sweep — for anyone who
    wants it, so it must not fall through to some floor.
    """
    monkeypatch.setattr(settings, "publish_retry_defer_seconds", 0.0)
    assert publishing_service.retry_defer_seconds(1) == 0.0
    assert publishing_service.retry_defer_seconds(3) == 0.0


def test_the_defaults_span_a_real_outage():
    """The numbers are the whole point, so pin what they add up to.

    Three attempts that all land inside ten minutes cannot survive anything a
    platform would call an incident. The series has to reach past the hour.
    """
    budget = settings.publish_max_retries
    total = sum(publishing_service.retry_defer_seconds(n) for n in range(1, budget))
    assert total >= 900


# --------------------------------------------------------------------------- #
# What execute() does with it                                                  #
# --------------------------------------------------------------------------- #


def test_a_retryable_failure_is_parked_rather_than_left_due(
    db, content, connected, failing
):
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    before = utcnow()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.SCHEDULED
    assert publication.scheduled_for is not None
    waited = as_aware(publication.scheduled_for) - before
    assert waited >= timedelta(seconds=settings.publish_retry_defer_seconds - 1)


def test_a_parked_retry_is_not_handed_back_to_the_sweep(
    db, content, connected, failing
):
    """The assertion the whole change rests on.

    ``due_publications`` is what the beat sweep asks, and before this the answer
    for a just-failed row was "yes, right now".
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication not in publishing_service.due_publications(db)


def test_the_sweep_picks_it_up_once_the_wait_is_over(db, content, connected, failing):
    """Parked is not dropped. A backoff nobody comes back from is a leak."""
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    later = as_aware(publication.scheduled_for) + timedelta(seconds=1)
    assert publication in publishing_service.due_publications(db, now=later)


def test_backing_off_does_not_buy_extra_attempts(db, content, connected, failing):
    """The budget is unchanged — only its spacing is.

    Worth pinning separately: it would be easy to park a row in a way that also
    stopped counting the attempt, and then a permanently broken platform would
    be retried forever instead of reaching a human.
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    for _ in range(settings.publish_max_retries):
        publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert publication.attempts == settings.publish_max_retries
    assert publication.scheduled_for is None
    assert content.status == ContentStatus.FAILED


def test_the_wait_is_visible_on_the_row(db, content, connected, failing):
    """The publications list is where somebody finds out why nothing happened.

    "devto is down" alone reads as terminal. The row says when it will try
    again, the same way a deferred rate limit does.
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert "devto is down" in publication.error
    assert "retrying in" in publication.error


def test_a_terminal_failure_is_not_parked(db, content, connected, monkeypatch):
    """Nothing to come back for, so no ``scheduled_for`` to mislead the UI."""
    from app.services.publishers.base import CredentialError
    from app.services.publishers.devto import DevToAdapter

    def publish(self, request, credentials):
        raise CredentialError("bad key")

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert publication.scheduled_for is None
    assert "retrying in" not in publication.error


def test_a_hand_retry_still_goes_out_immediately(db, content, connected, failing):
    """The parked row must not make "retry now" mean "retry in twenty minutes".

    ``routers.content.retry_publication`` clears ``scheduled_for`` for exactly
    this reason — a human clicking retry has information the backoff does not.
    """
    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    publishing_service.execute(db, publication)
    assert publication.scheduled_for is not None

    publication.status = PublicationStatus.PENDING
    publication.scheduled_for = None
    db.commit()

    assert publication in publishing_service.due_publications(db)


# --------------------------------------------------------------------------- #
# Rate limits reuse the same window                                            #
# --------------------------------------------------------------------------- #


def test_a_rate_limit_without_retry_after_escalates_too(
    db, content, connected, monkeypatch
):
    """A platform that will not say when is one to back away from.

    This used to defer by a flat sweep interval however many times it happened,
    which is the shape of request pattern a soft limit hardens against.
    """
    from app.services.publishers.devto import DevToAdapter

    def publish(self, request, credentials):
        raise RateLimited("devto said slow down", retry_after=None)

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()

    publishing_service.execute(db, publication)
    first = as_aware(publication.scheduled_for) - utcnow()

    publishing_service.execute(db, publication)
    second = as_aware(publication.scheduled_for) - utcnow()

    assert second > first


def test_the_last_deferral_does_not_outlive_the_publication(
    db, content, connected, monkeypatch
):
    """A row that has given up must not still look scheduled.

    The final attempt fails terminally, but the ``scheduled_for`` written by the
    deferral before it is still sitting there — a future time on a row nothing
    will ever come back for, which is what the calendar reads.
    """
    from app.services.publishers.devto import DevToAdapter

    def publish(self, request, credentials):
        raise RateLimited("slow down", retry_after=30.0)

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    for _ in range(settings.publish_max_retries):
        publishing_service.execute(db, publication)

    assert publication.status == PublicationStatus.FAILED
    assert publication.scheduled_for is None


def test_an_announced_outage_parks_the_row_until_the_platform_is_back(
    db, content, connected, monkeypatch
):
    """A 503 with a ``Retry-After`` reaches ``_defer``, not the default window.

    The whole chain, because this is where it used to break in the middle: the
    adapter's ``_translate`` turns the 503 into a ``RateLimited``, ``_is_retryable``
    sees a wait longer than the in-process ceiling and hands it up rather than
    sleeping a fraction of it, and ``execute`` parks the row at the platform's
    time. Before, the 503 was a plain ``PublishError`` the whole way down: three
    fast in-process replays into a platform that had just said it was down, then
    ``retry_defer_seconds`` — 300s against an announced 900.
    """
    from app.services.publishers import base
    from app.services.publishers.devto import DevToAdapter

    announced = 900.0
    assert announced > settings.publish_retry_max_backoff_seconds
    assert announced > settings.publish_retry_defer_seconds

    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        return httpx.Response(
            503,
            headers={"Retry-After": str(int(announced))},
            json={"error": "scheduled maintenance"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(base.httpx, "request", fake_request)
    monkeypatch.setattr(base, "_sleep", lambda seconds: pytest.fail(
        f"slept {seconds}s against a platform that asked for {announced}s"
    ))
    monkeypatch.setattr(
        DevToAdapter, "publish", lambda self, request, credentials: self._request(
            "POST", "https://dev.to/api/articles", json_body={}
        )
    )

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = utcnow()
    publishing_service.execute(db, publication)

    # One call: the loop declined to replay into an outage it was told about.
    assert calls == ["POST"]
    assert publication.status == PublicationStatus.SCHEDULED
    waited = as_aware(publication.scheduled_for) - before
    assert timedelta(seconds=announced - 5) <= waited <= timedelta(
        seconds=announced + 10
    )
    # The budget is spent one attempt at a time, not all at once.
    assert publication.attempts == 1


def test_an_announced_outage_is_still_capped(db, content, connected, monkeypatch):
    """A platform is allowed to say "next week"; Pulse is not allowed to wait.

    ``_defer``'s cap is what stops a hostile or mistaken header parking a row
    past the point anybody is still watching for it.
    """
    from app.services.publishers.devto import DevToAdapter

    forever = float(settings.publish_rate_limit_max_defer_seconds) * 10

    def publish(self, request, credentials):
        raise RateLimited("dev.to is unavailable (503)", retry_after=forever)

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = utcnow()
    publishing_service.execute(db, publication)

    waited = as_aware(publication.scheduled_for) - before
    assert waited <= timedelta(seconds=settings.publish_rate_limit_max_defer_seconds + 5)


def test_a_platform_that_says_when_is_still_obeyed(db, content, connected, monkeypatch):
    """The escalation is a fallback, not an override.

    Coming back sooner *or later* than a platform asked are different mistakes,
    and the first is the one that gets an account limited.
    """
    from app.services.publishers.devto import DevToAdapter

    def publish(self, request, credentials):
        raise RateLimited("slow down", retry_after=42.0)

    monkeypatch.setattr(DevToAdapter, "publish", publish)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    db.commit()
    before = utcnow()
    publishing_service.execute(db, publication)

    waited = as_aware(publication.scheduled_for) - before
    assert timedelta(seconds=41) <= waited <= timedelta(seconds=50)
