"""One row whose *write* fails must not end the sweep it is in.

Every periodic sweep in this tree wraps its per-row work in ``except Exception``
so that one bad row cannot stop the rest. That handler is written for a failure
coming from *outside* the database — a platform returning nonsense, an endpoint
refusing the connection — and against those it works.

A failed **commit** is the case it did not cover, and it breaks the handler in
two different ways depending on the sweep:

* ``collect_all_metrics`` and ``deliver_due`` never rolled back at all, so the
  session stayed poisoned and every row after the first failed too — on an
  error that says nothing about them.
* ``auto_select_headlines`` and ``release_approved_content`` did roll back, but
  *after* naming the row in the log line. Reading ``content.id`` off an expired
  instance needs a SELECT, and a session with a failed flush behind it refuses
  to emit one: the ``PendingRollbackError`` raised from inside the ``except``
  arm, escaped it, and ended the sweep on the failure it was written to absorb.

Each test here fails the write on the first of two rows and asserts the second
one is still processed. The failure is provoked with a row the database will
not take, which stands in for the production versions of the same thing — a
deadlock, a dropped connection, a constraint tripped by a concurrent writer.
What matters is that the commit raises, not why.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.security import hash_password
from app.services import (
    content_pipeline,
    headlines,
    link_check,
    mailer,
    publishing_service,
)
from app.services import digest as digest_service
from app.services import webhooks as webhooks_service
from app.tasks import (
    digest_tasks,
    headline_tasks,
    metrics_tasks,
    publish_tasks,
    webhook_tasks,
)


def _poison(session) -> None:
    """Leave *session* exactly as a failed commit leaves it.

    A metric with no publication trips ``NOT NULL``, so the flush raises and
    SQLAlchemy deactivates the transaction. Anything the session is asked for
    afterwards — including the SELECT behind an expired attribute — raises
    ``PendingRollbackError`` until somebody rolls back.
    """
    session.add(ContentMetric(publication_id=None, views=1))
    session.commit()


def _published(db, project, index: int) -> Publication:
    content = Content(
        project_id=project.id,
        title=f"Piece {index}",
        slug=f"piece-{index}",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow(),
        external_id=f"ext-{index}",
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return publication


# --------------------------------------------------------------------------- #
# collect_all_metrics                                                          #
# --------------------------------------------------------------------------- #


def test_metrics_sweep_polls_the_rest_after_one_row_fails_to_commit(
    db, project, monkeypatch, task_session
):
    """The sweep records the second publication even though the first blew up."""
    first = _published(db, project, 0)
    second = _published(db, project, 1)
    monkeypatch.setattr(metrics_tasks, "SessionLocal", task_session)

    seen: list[int] = []

    def fake_collect(session, publication, **_kwargs):
        # The shape of the real thing: build a snapshot, add it, commit.
        seen.append(publication.id)
        if publication.id == first.id:
            _poison(session)
        metric = ContentMetric(publication_id=publication.id, views=7)
        session.add(metric)
        session.commit()
        return metric

    monkeypatch.setattr(publishing_service, "collect_metrics", fake_collect)

    result = metrics_tasks.collect_all_metrics()

    assert seen == [first.id, second.id], "the sweep stopped at the failing row"
    assert result == {"polled": 2, "recorded": 1}
    recorded = db.scalars(
        select(ContentMetric).where(ContentMetric.publication_id == second.id)
    ).all()
    assert [m.views for m in recorded] == [7]


# --------------------------------------------------------------------------- #
# deliver_due                                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _no_dns(monkeypatch):
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


def _delivery(db, webhook, index: int) -> WebhookDelivery:
    row = WebhookDelivery(
        webhook_id=webhook.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload={"n": index},
        status=DeliveryStatus.PENDING,
        attempts=0,
        next_attempt_at=utcnow() - timedelta(minutes=5),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_webhook_sweep_delivers_the_rest_after_one_row_fails_to_commit(
    db, user, monkeypatch, task_session, _no_dns
):
    """A delivery whose outcome will not persist must not silence the queue."""
    webhook = Webhook(
        user_id=user.id,
        url="https://hooks.example.test/herald",
        encrypted_secret=webhooks_service.store_secret(
            webhooks_service.generate_secret()
        ),
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        is_active=True,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)

    first = _delivery(db, webhook, 0)
    second = _delivery(db, webhook, 1)
    monkeypatch.setattr(webhook_tasks, "SessionLocal", task_session)

    seen: list[int] = []

    def fake_deliver(session, delivery):
        seen.append(delivery.id)
        if delivery.id == first.id:
            _poison(session)
        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = utcnow()
        session.commit()
        return delivery

    monkeypatch.setattr(webhooks_service, "deliver", fake_deliver)

    result = webhook_tasks.deliver_due()

    assert seen == [first.id, second.id], "the sweep stopped at the failing row"
    assert result == {"attempted": 2, "delivered": 1}
    db.refresh(second)
    assert second.status is DeliveryStatus.DELIVERED


# --------------------------------------------------------------------------- #
# auto_select_headlines                                                        #
# --------------------------------------------------------------------------- #


def test_headline_sweep_reaches_the_rest_after_one_row_fails_to_commit(
    db, project, monkeypatch, task_session
):
    """The commit is the task's, so the failure lands in the task's handler."""
    project.auto_headline_winner = True
    db.commit()

    contents = []
    for index in range(2):
        content = Content(
            project_id=project.id,
            title=f"Headline {index}",
            slug=f"headline-{index}",
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.PUBLISHED,
            body_markdown="body",
        )
        db.add(content)
        db.commit()
        db.refresh(content)
        contents.append(content)

    monkeypatch.setattr(headline_tasks, "SessionLocal", task_session)
    monkeypatch.setattr(
        headline_tasks, "_candidates", lambda db, now=None: list(contents)
    )

    seen: list[int] = []

    def fake_auto_select(content, session):
        seen.append(content.id)
        if content.id == contents[0].id:
            # A write the sweep's own ``db.commit()`` will choke on.
            session.add(ContentMetric(publication_id=None, views=1))
        content.title = f"Winner {content.id}"
        return object(), True

    monkeypatch.setattr(headlines, "auto_select", fake_auto_select)

    result = headline_tasks.auto_select_headlines()

    assert seen == [c.id for c in contents], "the sweep stopped at the failing row"
    assert result == {"considered": 2, "swapped": 1}
    db.refresh(contents[1])
    assert contents[1].title == f"Winner {contents[1].id}"


# --------------------------------------------------------------------------- #
# release_approved_content                                                     #
# --------------------------------------------------------------------------- #


def test_release_sweep_reaches_the_rest_after_one_row_fails_to_commit(
    db, project, monkeypatch, task_session
):
    """One piece that cannot be released must not strand the ones behind it."""
    project.autopilot_mode = AutopilotMode.AUTO
    db.commit()

    contents = []
    for index in range(2):
        content = Content(
            project_id=project.id,
            title=f"Approved {index}",
            slug=f"approved-{index}",
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.APPROVED,
            body_markdown="body",
        )
        db.add(content)
        db.commit()
        db.refresh(content)
        contents.append(content)

    monkeypatch.setattr(publish_tasks, "SessionLocal", task_session)

    seen: list[int] = []

    def fake_release(session, content):
        seen.append(content.id)
        if content.id == contents[0].id:
            _poison(session)
        content.status = ContentStatus.PUBLISHED
        session.commit()
        return True

    monkeypatch.setattr(content_pipeline, "release_approved", fake_release)

    result = publish_tasks.release_approved_content()

    assert sorted(seen) == sorted(c.id for c in contents), (
        "the sweep stopped at the failing row"
    )
    assert result == {"found": 2, "released": 1}


# --------------------------------------------------------------------------- #
# send_weekly_digests                                                          #
# --------------------------------------------------------------------------- #


def test_digest_sweep_mails_the_rest_after_one_user_fails(
    db, user, monkeypatch, task_session
):
    """One subscriber's bad week must not cancel everyone else's mail.

    The last sweep in the tree without a rollback in its handler. Building a
    digest only reads, so the failure this stands in for is a read that raises —
    a dropped connection, a statement timeout on the metrics history — and the
    session is left just as unusable either way. Without the rollback the second
    subscriber's build raised ``PendingRollbackError`` on the first user's error,
    and the sweep logged a line blaming them for it.
    """
    second = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(second)
    db.commit()
    db.refresh(second)

    monkeypatch.setattr(digest_tasks, "SessionLocal", task_session)
    monkeypatch.setattr(mailer, "configured", lambda: True)

    seen: list[int] = []

    def fake_send(session, recipient, **_kwargs):
        seen.append(recipient.id)
        if recipient.id == user.id:
            _poison(session)
        return True

    monkeypatch.setattr(digest_service, "send", fake_send)

    result = digest_tasks.send_weekly_digests()

    assert sorted(seen) == sorted([user.id, second.id]), (
        "the sweep stopped at the failing user"
    )
    assert result == {"considered": 2, "sent": 1}
