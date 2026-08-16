"""The engagement-threshold webhook, and the ways it could fire wrongly.

Herald has collected engagement numbers for a long time and never told anybody
about them. This is the notification: a per-project threshold, and one
``content.engagement_threshold`` webhook when a piece passes it.

The interesting failures are all about *firing wrongly* rather than not firing:

* **Twice.** ``engagement >= threshold`` becomes true and then stays true, so an
  unlatched check is a webhook every metrics sweep for the rest of the piece's
  life — which is how a useful alert gets muted.

* **Too early, from one platform's share.** A piece publishes to several
  platforms and the sweep visits each as its own row. Checking per row would
  announce a piece on whichever platform was polled first, with only that
  platform's number.

* **From arithmetic rather than from readers.** ``content_metrics`` is
  append-only, so summing every row counts the same interactions once per poll
  and crosses any threshold eventually whether or not anybody engaged.

* **When nobody asked.** The default is ``0``, and zero has to mean *never* — a
  threshold nobody chose is an alert that fires at the wrong time.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import SUBSCRIBABLE_EVENTS, Webhook, WebhookDelivery, WebhookEvent
from app.services import engagement_alerts
from app.services import webhooks as webhook_service

V1 = "/api/v1"


@pytest.fixture
def hook(db, user) -> Webhook:
    """An endpoint subscribed to the new event."""
    row = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        events=[WebhookEvent.ENGAGEMENT_THRESHOLD.value],
        encrypted_secret=webhook_service.store_secret("s3cret"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    """Queue deliveries; never try to send them.

    ``emit`` hands the ids straight to a dispatcher, which in tests runs inline
    and would make a real outbound request. The rows are what this file is
    about.
    """
    monkeypatch.setattr(webhook_service, "dispatch", lambda ids: None)


def _piece(db, project, *, title="Shipped", status=ContentStatus.PUBLISHED) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.CHANGELOG,
        status=status,
        title=title,
        slug=title.lower().replace(" ", "-"),
        body_markdown="word " * 60,
        excerpt="Shipped.",
        published_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def _publish(db, content, platform=Platform.DEVTO) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        external_id=f"ext-{content.id}-{platform.value}",
        external_url=f"https://{platform.value}.example.com/{content.slug}",
    )
    db.add(row)
    db.flush()
    return row


def _snapshot(db, publication, *, reactions=0, comments=0, shares=0, views=0, ago_hours=0):
    row = ContentMetric(
        publication_id=publication.id,
        captured_at=utcnow() - timedelta(hours=ago_hours),
        views=views,
        reactions=reactions,
        comments=comments,
        shares=shares,
    )
    db.add(row)
    db.flush()
    return row


def _deliveries(db) -> list[WebhookDelivery]:
    return (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.event == WebhookEvent.ENGAGEMENT_THRESHOLD)
        .all()
    )


# --------------------------------------------------------------------------- #
# Crossing                                                                     #
# --------------------------------------------------------------------------- #


def test_a_piece_that_passes_the_threshold_is_announced(db, project, hook):
    project.engagement_threshold = 50
    content = _piece(db, project)
    publication = _publish(db, content)
    _snapshot(db, publication, reactions=40, comments=15, views=900)
    db.commit()

    crossings = engagement_alerts.evaluate(db, [content.id])

    assert [c.content_id for c in crossings] == [content.id]
    assert crossings[0].engagement == 55
    assert crossings[0].threshold == 50
    assert crossings[0].views == 900
    assert crossings[0].platforms == ["devto"]

    sent = _deliveries(db)
    assert len(sent) == 1
    data = sent[0].payload["data"]
    assert sent[0].payload["event"] == "content.engagement_threshold"
    # The threshold travels with the number, so a receiver watching several
    # projects can tell which bar was cleared without its own copy of the
    # settings.
    assert (data["threshold"], data["engagement"], data["views"]) == (50, 55, 900)
    assert data["content"]["id"] == content.id
    assert data["content"]["title"] == "Shipped"


def test_the_boundary_is_inclusive(db, project, hook):
    """Exactly the threshold counts as passing it.

    Chosen rather than inherited: somebody who types 50 means "tell me at 50".
    """
    project.engagement_threshold = 50
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=50)
    db.commit()

    assert len(engagement_alerts.evaluate(db, [content.id])) == 1


def test_a_piece_short_of_the_threshold_is_left_alone(db, project, hook):
    project.engagement_threshold = 50
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=49, views=100_000)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []
    assert _deliveries(db) == []
    # Views are deliberately not what the threshold measures — the platforms
    # disagree about what a view is, so a bar set on them means something
    # different on each one and nothing across them.
    assert content.engagement_notified_at is None


def test_the_numbers_from_every_platform_are_added_up(db, project, hook):
    """One piece, several platforms — the piece's number is the sum of theirs."""
    project.engagement_threshold = 100
    content = _piece(db, project)
    _snapshot(db, _publish(db, content, Platform.DEVTO), reactions=60, views=500)
    _snapshot(db, _publish(db, content, Platform.HASHNODE), reactions=45, views=300)
    db.commit()

    crossings = engagement_alerts.evaluate(db, [content.id])
    assert crossings[0].engagement == 105
    assert crossings[0].views == 800
    assert crossings[0].platforms == ["devto", "hashnode"]


def test_only_the_latest_snapshot_of_each_publication_counts(db, project, hook):
    """The append-only trap, which would make any threshold cross eventually.

    Three polls of a post sitting at 30 reactions is 30, not 90. Summing the
    series would announce a piece nobody engaged with, on a schedule set by how
    often the sweep runs.
    """
    project.engagement_threshold = 80
    content = _piece(db, project)
    publication = _publish(db, content)
    _snapshot(db, publication, reactions=30, views=100, ago_hours=3)
    _snapshot(db, publication, reactions=30, views=100, ago_hours=2)
    _snapshot(db, publication, reactions=30, views=100, ago_hours=1)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []

    # And when the *latest* one really does clear the bar, it fires.
    _snapshot(db, publication, reactions=85, views=400)
    db.commit()
    assert len(engagement_alerts.evaluate(db, [content.id])) == 1


def test_a_publication_that_is_not_live_contributes_nothing(db, project, hook):
    """A cancelled or failed copy must not freeze its last reading into the total."""
    project.engagement_threshold = 100
    content = _piece(db, project)
    live = _publish(db, content, Platform.DEVTO)
    _snapshot(db, live, reactions=60)

    withdrawn = _publish(db, content, Platform.HASHNODE)
    withdrawn.status = PublicationStatus.CANCELLED
    _snapshot(db, withdrawn, reactions=90)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []


# --------------------------------------------------------------------------- #
# Firing once                                                                  #
# --------------------------------------------------------------------------- #


def test_a_piece_is_announced_once_however_often_the_sweep_runs(db, project, hook):
    """The latch. Without it this is a webhook every few hours, forever."""
    project.engagement_threshold = 10
    content = _piece(db, project)
    publication = _publish(db, content)
    _snapshot(db, publication, reactions=50)
    db.commit()

    assert len(engagement_alerts.evaluate(db, [content.id])) == 1
    stamped = content.engagement_notified_at
    assert stamped is not None

    # The numbers keep climbing, as they do.
    for reactions in (80, 200, 5000):
        _snapshot(db, publication, reactions=reactions)
        db.commit()
        assert engagement_alerts.evaluate(db, [content.id]) == []

    assert len(_deliveries(db)) == 1
    assert content.engagement_notified_at == stamped


def test_lowering_the_threshold_afterwards_does_not_re_announce(db, project, hook):
    """A setting moving is not news about the piece."""
    project.engagement_threshold = 10
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=50)
    db.commit()
    engagement_alerts.evaluate(db, [content.id])

    project.engagement_threshold = 1
    db.commit()
    assert engagement_alerts.evaluate(db, [content.id]) == []
    assert len(_deliveries(db)) == 1


# --------------------------------------------------------------------------- #
# Not firing                                                                   #
# --------------------------------------------------------------------------- #


def test_zero_means_never(db, project, hook):
    """The default, and the reason it is the default.

    An alert nobody chose a number for fires at the wrong time, gets muted, and
    takes the alerts that mattered with it.
    """
    assert project.engagement_threshold == 0
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=10_000)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []
    assert _deliveries(db) == []


def test_a_piece_that_is_not_published_is_not_announced(db, project, hook):
    """Whatever the numbers say, a draft has no audience to have impressed."""
    project.engagement_threshold = 5
    content = _piece(db, project, status=ContentStatus.DRAFT)
    _snapshot(db, _publish(db, content), reactions=500)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []


def test_an_empty_batch_asks_the_database_nothing(db, project, hook, sql_log):
    project.engagement_threshold = 5
    db.commit()
    sql_log.clear()
    assert engagement_alerts.evaluate(db, []) == []
    assert sql_log == []


def test_a_piece_outside_the_batch_is_not_evaluated(db, project, hook):
    """The sweep hands over what it just polled; nothing else is this run's business."""
    project.engagement_threshold = 10
    mine = _piece(db, project, title="Mine")
    theirs = _piece(db, project, title="Also mine but unpolled")
    _snapshot(db, _publish(db, mine), reactions=50)
    _snapshot(db, _publish(db, theirs), reactions=50)
    db.commit()

    crossings = engagement_alerts.evaluate(db, [mine.id])
    assert [c.content_id for c in crossings] == [mine.id]
    assert theirs.engagement_notified_at is None


def test_a_subscriber_to_other_events_is_not_told(db, project, hook):
    """``emit`` fans out by subscription, and this is the new event's turn."""
    hook.events = [WebhookEvent.CONTENT_PUBLISHED.value]
    project.engagement_threshold = 10
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=50)
    db.commit()

    # It still crosses and still latches — the piece did what it did, whether or
    # not anybody was listening.
    assert len(engagement_alerts.evaluate(db, [content.id])) == 1
    assert _deliveries(db) == []
    assert content.engagement_notified_at is not None


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #


def test_the_event_is_subscribable_and_described(client, auth):
    assert WebhookEvent.ENGAGEMENT_THRESHOLD in SUBSCRIBABLE_EVENTS
    rows = client.get(f"{V1}/webhooks/events", headers=auth).json()
    described = {row["event"]: row["description"] for row in rows}
    assert described["content.engagement_threshold"]


def test_the_threshold_is_a_project_setting_that_round_trips(client, auth, project, db):
    resp = client.patch(
        f"{V1}/projects/{project.id}", headers=auth, json={"engagement_threshold": 250}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["engagement_threshold"] == 250

    db.refresh(project)
    assert project.engagement_threshold == 250

    assert client.get(f"{V1}/projects/{project.id}", headers=auth).json()[
        "engagement_threshold"
    ] == 250


def test_a_negative_threshold_is_refused(client, auth, project):
    resp = client.patch(
        f"{V1}/projects/{project.id}", headers=auth, json={"engagement_threshold": -1}
    )
    assert resp.status_code == 422


def test_the_metrics_sweep_runs_the_check_at_its_tail(db, project, hook, monkeypatch):
    """The integration: the sweep collects, then evaluates what it collected.

    Stubs the poller — this is about the wiring, not about any platform — and
    asserts on the count the task reports, which is the number an operator sees
    in the log.
    """
    from app.models.platform_connection import ConnectionStatus, PlatformConnection
    from app.services import publishers
    from app.services.crypto import encrypt_credentials
    from app.services.publishers.base import MetricsSnapshot
    from app.tasks import metrics_tasks

    project.engagement_threshold = 20
    content = _piece(db, project)
    _publish(db, content)
    db.add(
        PlatformConnection(
            user_id=project.user_id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()

    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO),
        "fetch_metrics",
        lambda external_id, credentials: MetricsSnapshot(views=500, reactions=25),
    )

    class _NoClose:
        def __init__(self, session):
            self._session = session

        def __call__(self):
            return self

        def __getattr__(self, name):
            return getattr(self._session, name)

        def close(self):
            pass

    monkeypatch.setattr(metrics_tasks, "SessionLocal", _NoClose(db))

    result = metrics_tasks.collect_all_metrics()
    assert result["recorded"] == 1
    assert result["crossings"] == 1
    assert len(_deliveries(db)) == 1
    assert as_aware(content.engagement_notified_at) <= utcnow()


def test_a_broken_threshold_check_does_not_fail_the_whole_sweep(
    db, project, hook, monkeypatch
):
    """The tail must not be able to lose a sweep that polled everything fine.

    Same contract as ``webhooks.emit``: a notification is a side channel, and a
    side channel that can fail the work it reports on is worse than no channel.
    """
    from app.tasks import metrics_tasks

    def _boom(*args, **kwargs):
        raise RuntimeError("the notification exploded")

    monkeypatch.setattr(metrics_tasks.engagement_alerts, "evaluate", _boom)

    class _NoClose:
        def __init__(self, session):
            self._session = session

        def __call__(self):
            return self

        def __getattr__(self, name):
            return getattr(self._session, name)

        def close(self):
            pass

    monkeypatch.setattr(metrics_tasks, "SessionLocal", _NoClose(db))

    result = metrics_tasks.collect_all_metrics()
    assert result["crossings"] == 0


def test_a_piece_with_no_live_publication_is_not_announced(db, project, hook):
    """Nothing has been polled, so there is no number — not a zero to compare.

    Reachable in production: the sweep batches by piece, and a piece can be in
    the batch on the strength of one platform while another was archived out
    from under it between the poll and the check.
    """
    project.engagement_threshold = 1
    content = _piece(db, project)
    db.commit()

    assert engagement_alerts.evaluate(db, [content.id]) == []
    assert content.engagement_notified_at is None


def test_a_latch_that_will_not_commit_leaves_the_piece_for_the_next_sweep(
    db, project, hook, monkeypatch
):
    """The "never raises" contract, at the one place it could bite.

    The latch is written before the webhook is queued precisely so that a
    failure here loses a notification rather than the guarantee that there is
    only ever one of them. What it must not do is escape into the sweep.
    """
    project.engagement_threshold = 10
    content = _piece(db, project)
    _snapshot(db, _publish(db, content), reactions=50)
    db.commit()

    calls = {"n": 0}
    real_commit = db.commit

    def _fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the commit exploded")
        return real_commit()

    monkeypatch.setattr(db, "commit", _fail_once)
    assert engagement_alerts.evaluate(db, [content.id]) == []
    monkeypatch.undo()

    # Nothing was announced, and nothing was latched — so the next sweep tries
    # again, which is the outcome this ordering was chosen for.
    assert _deliveries(db) == []
    assert content.engagement_notified_at is None
    assert len(engagement_alerts.evaluate(db, [content.id])) == 1
