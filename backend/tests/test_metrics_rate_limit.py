"""What the metrics sweep does when a platform says "stop".

The numbers Pulse polls are cumulative counters read a few times a day, so a
429 costs nothing to obey: whatever the platform would have said is still true
at the next sweep. Ignoring it costs a great deal — the sweep walks one
publication at a time, so an account with forty live Dev.to posts answers a
single rate limit with thirty-nine more refused requests, which is how a soft
limit turns into a blocked key.

The budget belongs to the *credential*, so the unit that stands down is the
``(user, platform)`` pair. One user hitting a limit must not stop another user's
polling on the same platform, and it must not stop the same user's polling on a
different one.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import publishing_service
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import MetricsSnapshot, PublishError, RateLimited


@pytest.fixture
def connected(db, user):
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()


def _published(db, project: Project, external_id: str) -> Publication:
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        title=f"Post {external_id}",
        slug=f"post-{external_id}",
        body_markdown="Words.",
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id=external_id,
        external_url=f"https://dev.to/a/{external_id}",
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return publication


@pytest.fixture
def poller(monkeypatch):
    """Replace Dev.to's metrics call; returns the external ids it was asked for."""
    asked: list[str] = []

    def install(*, fail_after: int = 0):
        def fetch_metrics(external_id, credentials):
            asked.append(external_id)
            if fail_after and len(asked) > fail_after:
                raise RateLimited("Dev.to rate-limited the request", retry_after=900)
            return MetricsSnapshot(views=10)

        from app.services import publishers

        monkeypatch.setattr(
            publishers.get_adapter(Platform.DEVTO), "fetch_metrics", fetch_metrics
        )
        return asked

    return install


def test_a_rate_limit_stands_the_account_down_for_the_rest_of_the_sweep(
    db, user, project, connected, poller
):
    asked = poller(fail_after=1)
    publications = [_published(db, project, str(n)) for n in range(4)]

    seen: set[publishing_service.RateLimitKey] = set()
    results = [
        publishing_service.collect_metrics(db, p, rate_limited=seen)
        for p in publications
    ]

    # One success, one refused, and then no further requests at all.
    assert asked == ["0", "1"]
    assert [r is not None for r in results] == [True, False, False, False]
    assert seen == {(user.id, Platform.DEVTO)}


def test_without_the_sweep_memory_each_call_stands_alone(
    db, project, connected, poller
):
    """The single-post refresh button has no sweep to protect — it just asks."""
    asked = poller(fail_after=0)
    publication = _published(db, project, "solo")

    assert publishing_service.collect_metrics(db, publication) is not None
    assert asked == ["solo"]


def test_a_stood_down_account_makes_no_request_at_all(db, user, project, connected, poller):
    """Not "asks and swallows the error" — the point is the request is not made."""
    asked = poller()
    publication = _published(db, project, "1")

    seen = {(user.id, Platform.DEVTO)}
    assert publishing_service.collect_metrics(db, publication, rate_limited=seen) is None
    assert asked == []


def test_one_users_rate_limit_does_not_stand_down_another(
    db, user, project, connected, poller
):
    """The budget belongs to the credential, not to the platform."""
    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.flush()
    other_project = Project(user_id=other.id, name="Theirs", slug="theirs")
    db.add(other_project)
    db.add(
        PlatformConnection(
            user_id=other.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "other-key"}),
        )
    )
    db.commit()

    asked = poller()
    theirs = _published(db, other_project, "theirs-1")

    seen = {(user.id, Platform.DEVTO)}
    assert publishing_service.collect_metrics(db, theirs, rate_limited=seen) is not None
    assert asked == ["theirs-1"]


def test_an_ordinary_failure_does_not_stand_the_account_down(
    db, project, connected, monkeypatch, poller
):
    """A 500 is one post having a bad moment, not a budget being spent."""
    asked: list[str] = []

    def fetch_metrics(external_id, credentials):
        asked.append(external_id)
        raise PublishError("Dev.to returned 500")

    from app.services import publishers

    monkeypatch.setattr(
        publishers.get_adapter(Platform.DEVTO), "fetch_metrics", fetch_metrics
    )
    publications = [_published(db, project, str(n)) for n in range(3)]

    seen: set[publishing_service.RateLimitKey] = set()
    for publication in publications:
        assert publishing_service.collect_metrics(db, publication, rate_limited=seen) is None

    assert asked == ["0", "1", "2"]
    assert seen == set()


def test_the_beat_task_shares_one_memory_across_the_whole_sweep(
    db, user, project, connected, poller, monkeypatch
):
    """End to end through ``collect_all_metrics``, which is what runs in prod."""
    from app.tasks import metrics_tasks

    asked = poller(fail_after=1)
    for n in range(5):
        _published(db, project, str(n))

    class _Session:
        """Hand the task the test's session and ignore its close()."""

        def __call__(self):
            return self

        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr(metrics_tasks, "SessionLocal", _Session())

    result = metrics_tasks.collect_all_metrics()

    assert result["polled"] == 5
    assert result["recorded"] == 1
    assert asked == ["0", "1"]
