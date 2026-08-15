"""Deactivating an account stops the background work that holds its keys.

Deactivation is Herald's off switch, and the codebase is unusually consistent
about what it means: the account's tokens stop working
(:func:`app.deps.get_current_user`), its preview links stop resolving
(:func:`app.services.preview_links.resolve`), its inbound webhooks write nothing
(:func:`app.services.triggers.fire`), and the scan, release, headline and digest
sweeps all carry ``User.is_active`` in the query that chooses their rows.

Two paths did not, and both of them act *outward* — with the account's own
stored platform credentials, which is what makes them worth separating from the
ordinary "stale row" bug:

* **The metrics sweep.** ``collect_all_metrics`` selected every published
  publication on the install, and ``collect_metrics`` takes ``user_id`` for the
  express purpose of looking the account's credentials up. A switched-off
  account went on making authenticated requests to Dev.to and Hashnode under its
  own tokens, every few hours, for as long as the install ran.

* **Publishing, which is the sharper one, because it writes.**
  ``release_approved_content`` will not *arm* anything for a deactivated
  account — that gate was there. But ``due_publications`` selects on status and
  time alone, so a row armed while the account was live outlived the switch-off:
  a piece scheduled for next Tuesday went out on Tuesday, to a platform, on
  behalf of an account Herald had been told to stop. Deactivating mid-flight is
  not an exotic case — it is what you do first when an account is compromised or
  its owner has left.

The gate for the second one sits in :func:`app.services.publishing_service.execute`,
beside the archived-content check, which already documents itself as the last
thing to see the piece before a platform is contacted and the net for "any
future path that forgets".
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import publishers, publishing_service
from app.tasks import metrics_tasks


def _piece(db, project, *, status: ContentStatus, index: int = 0) -> Content:
    content = Content(
        project_id=project.id,
        title=f"Piece {index}",
        slug=f"piece-{index}",
        content_type=ContentType.ANNOUNCEMENT,
        status=status,
        body_markdown="body text here",
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


# --------------------------------------------------------------------------- #
# Publishing: the account is switched off while a row is already armed         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def armed(db, project, connect, monkeypatch):
    """A publication armed and due, and a record of what reaches the platform.

    The adapter is stubbed at ``publish``: this file is about whether a platform
    is contacted at all, not about what one answers.
    """
    connect(Platform.DEVTO)
    content = _piece(db, project, status=ContentStatus.APPROVED)
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    reached: list[str] = []
    adapter = publishers.get_adapter(Platform.DEVTO)

    def _publish(self, request, credentials):
        reached.append(request.title)
        raise publishers.base.NotConnected("stub: nothing is really published here")

    monkeypatch.setattr(type(adapter), "publish", _publish)
    return publication, reached


def test_an_active_account_still_publishes(db, armed, user):
    """The guard on the test below — otherwise it passes for the wrong reason."""
    publication, reached = armed
    assert user.is_active

    publishing_service.execute(db, publication)

    assert reached, "an active account's armed publication should reach the platform"


def test_a_deactivated_account_does_not_reach_the_platform(db, armed, user):
    """The bug, and the one that writes.

    The row was armed while the account was live, which is the only way to get
    here — arming is already gated. What changed underneath it is the account.
    """
    publication, reached = armed
    user.is_active = False
    db.commit()

    publishing_service.execute(db, publication)

    assert reached == [], (
        "a deactivated account published to a platform, under its own stored "
        "credentials, after Herald was told to switch it off"
    )


def test_the_row_is_cancelled_rather_than_left_to_retry(db, armed, user):
    """Nothing failed, so nothing should be waiting to be tried again.

    ``failed`` would be both untrue and load-bearing: a failed row with a
    schedule on it is what the retry paths pick back up.
    """
    publication, _reached = armed
    user.is_active = False
    db.commit()

    publishing_service.execute(db, publication)
    db.refresh(publication)

    assert publication.status == PublicationStatus.CANCELLED
    assert publication.scheduled_for is None
    assert publication.error == publishing_service.DEACTIVATED_ERROR


def test_the_sweep_that_arms_rows_was_already_guarded():
    """Stated so the gate above is not mistaken for the only one, and so that
    deleting the older one shows up here too.

    ``release_approved_content`` has always refused to arm anything for a
    deactivated account. That is precisely why the bug needed a row armed
    *before* the switch-off to reach a platform at all — and why the fix had to
    go somewhere that sees rows armed earlier, rather than beside this.
    """
    import inspect

    from app.tasks import publish_tasks

    assert "User.is_active" in inspect.getsource(publish_tasks.release_approved_content)


# --------------------------------------------------------------------------- #
# Metrics: polling with a switched-off account's credentials                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def polled(db, project, monkeypatch) -> list[int]:
    """One published post, and a record of what the sweep decides to poll."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr(metrics_tasks, "SessionLocal", NoCloseProxy)

    content = _piece(db, project, status=ContentStatus.PUBLISHED, index=2)
    db.add(
        Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow(),
            external_id="ext-1",
        )
    )
    db.commit()

    seen: list[int] = []

    def _record(db_, publication, *, user_id=None, rate_limited=None):
        seen.append(publication.id)
        return None

    monkeypatch.setattr(metrics_tasks.publishing_service, "collect_metrics", _record)
    return seen


def test_an_active_account_is_polled(polled, user):
    """The guard on the test below."""
    assert user.is_active

    result = metrics_tasks.collect_all_metrics()

    assert len(polled) == 1
    assert result["polled"] == 1


def test_a_deactivated_account_is_not_polled(db, polled, user):
    """The post is still published and its row is untouched — the only thing
    that changed is that its owner has been switched off."""
    user.is_active = False
    db.commit()

    result = metrics_tasks.collect_all_metrics()

    assert polled == [], (
        "a deactivated account's posts were polled — with that account's own "
        "stored platform credentials, which is the part that matters"
    )
    assert result == {"polled": 0, "recorded": 0}


def test_a_paused_project_is_still_polled(db, polled, project):
    """Deliberately *not* symmetrical, and the asymmetry is the point.

    Pausing a project says "write nothing new for this". It does not say "stop
    counting what already went out", and the views still accruing on its posts
    are its owner's numbers to come back to. The sweeps that write check this
    flag; this one reads.
    """
    project.is_active = False
    db.commit()

    metrics_tasks.collect_all_metrics()

    assert polled != [], "pausing a project should not discard its analytics"
