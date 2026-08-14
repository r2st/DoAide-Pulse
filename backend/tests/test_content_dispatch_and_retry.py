"""Handing a publication to a worker, and asking for it a second time.

``_dispatch`` is four lines with three outcomes, and the one that matters most
is the one nothing else exercises: the broker refusing the handoff. Losing a
publish because Redis is down would be a silent failure of the button the user
just pressed, so it falls through to running inline. That fallback is only ever
correct if it actually happens, which is what the first half of this file pins.

The second half is ``POST /retry/{publication_id}``, whose job is to undo a
failure — status, attempt count, error text, and the backoff parking slot — and
then dispatch. Missing any one of those leaves a retry that looks armed and
never runs.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.routers import content as content_router


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="A piece",
        slug="a-piece",
        body_markdown="Body.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def failed_publication(db, content) -> Publication:
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        attempts=3,
        error="dev.to said 503",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# _dispatch                                                                    #
# --------------------------------------------------------------------------- #


class _FakeTask:
    """Stand-in for the celery task, recording both call styles separately."""

    def __init__(self, delay_error: Exception | None = None):
        self.delayed: list[int] = []
        self.ran_inline: list[int] = []
        self._delay_error = delay_error

    def delay(self, publication_id: int) -> None:
        if self._delay_error is not None:
            raise self._delay_error
        self.delayed.append(publication_id)

    def __call__(self, publication_id: int) -> None:
        self.ran_inline.append(publication_id)


@pytest.fixture
def fake_publish(monkeypatch):
    """Replace ``publish_tasks.publish_one``; ``_dispatch`` imports it lazily."""
    from app.tasks import publish_tasks

    def _install(task: _FakeTask) -> _FakeTask:
        monkeypatch.setattr(publish_tasks, "publish_one", task)
        return task

    return _install


def test_with_a_worker_configured_every_id_is_handed_to_the_broker(
    fake_publish, monkeypatch
):
    monkeypatch.setattr(content_router.settings, "celery_enabled", True)
    task = fake_publish(_FakeTask())

    content_router._dispatch([1, 2, 3])

    assert task.delayed == [1, 2, 3]
    assert task.ran_inline == [], "the broker took it; nothing should run inline"


def test_without_a_worker_the_publish_runs_inline(fake_publish, monkeypatch):
    """Single-process deployments and the test suite take this path."""
    monkeypatch.setattr(content_router.settings, "celery_enabled", False)
    task = fake_publish(_FakeTask())

    content_router._dispatch([7, 8])

    assert task.ran_inline == [7, 8]
    assert task.delayed == []


def test_a_broker_that_refuses_the_handoff_falls_back_to_publishing_inline(
    fake_publish, monkeypatch, caplog
):
    """Redis being down must not lose the publish the user just asked for."""
    monkeypatch.setattr(content_router.settings, "celery_enabled", True)
    task = fake_publish(_FakeTask(delay_error=OSError("connection refused")))

    with caplog.at_level("WARNING"):
        content_router._dispatch([11, 12])

    assert task.ran_inline == [11, 12]
    assert task.delayed == []
    # And it is not silent — an operator needs to know the broker is gone.
    assert "connection refused" in caplog.text


def test_the_fallback_resumes_where_the_broker_stopped(fake_publish, monkeypatch):
    """A broker that dies mid-batch drops nothing and repeats nothing.

    The ids ``delay`` already accepted belong to a worker. Restarting the inline
    loop from the top would run the whole publish attempt for them a second time
    on the request thread — and the only thing standing between that and a
    duplicate post is ``publish_one``'s conditional UPDATE, a layer away. This is
    the same accounting ``webhooks.dispatch`` does.
    """
    monkeypatch.setattr(content_router.settings, "celery_enabled", True)

    class _DiesOnTheSecond(_FakeTask):
        def delay(self, publication_id: int) -> None:
            if len(self.delayed) == 1:
                raise OSError("broker gone")
            self.delayed.append(publication_id)

    task = fake_publish(_DiesOnTheSecond())

    content_router._dispatch([1, 2, 3])

    assert task.delayed == [1], "the first was accepted before the broker died"
    assert task.ran_inline == [2, 3], "and only the unaccepted remainder runs here"


def test_a_broker_that_dies_on_the_last_id_leaves_nothing_for_the_fallback(
    fake_publish, monkeypatch
):
    """The boundary: every id but one was accepted, so one id runs inline.

    Worth pinning separately because an off-by-one in the resume index is
    invisible in the middle of a batch and shows up here as either a re-publish
    of the whole batch or a silently dropped tail.
    """
    monkeypatch.setattr(content_router.settings, "celery_enabled", True)

    class _DiesOnTheThird(_FakeTask):
        def delay(self, publication_id: int) -> None:
            if len(self.delayed) == 2:
                raise OSError("broker gone")
            self.delayed.append(publication_id)

    task = fake_publish(_DiesOnTheThird())

    content_router._dispatch([1, 2, 3])

    assert task.delayed == [1, 2]
    assert task.ran_inline == [3]


def test_one_publication_blowing_up_inline_does_not_strand_the_rest(
    fake_publish, monkeypatch, caplog
):
    """``publish_one`` records its own failures, so reaching here is off-contract.

    The remaining ids are unrelated publications whose rows are already
    committed. Letting the first exception escape the loop would leave them
    queued with nobody to run them, and would surface to the caller as a 500 on
    a publish that had in fact already been persisted.
    """
    monkeypatch.setattr(content_router.settings, "celery_enabled", False)

    class _ExplodesOnTheFirst(_FakeTask):
        def __call__(self, publication_id: int) -> None:
            if publication_id == 1:
                raise RuntimeError("adapter imploded")
            self.ran_inline.append(publication_id)

    task = fake_publish(_ExplodesOnTheFirst())

    with caplog.at_level("ERROR"):
        content_router._dispatch([1, 2, 3])

    assert task.ran_inline == [2, 3]
    assert "adapter imploded" in caplog.text


def test_an_empty_batch_never_imports_the_task_module(monkeypatch):
    """The early return happens before the lazy import, as in ``webhooks``."""
    import builtins

    _real_import = builtins.__import__

    def explode(name, globals=None, locals=None, fromlist=(), level=0):
        # ``from app.tasks import publish_tasks`` arrives as name="app.tasks"
        # with the attribute in the fromlist, not as a dotted name.
        if name == "app.tasks" and "publish_tasks" in (fromlist or ()):
            raise AssertionError("an empty batch must not reach the import")
        return _real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", explode)

    content_router._dispatch([])


def test_dispatching_nothing_touches_neither_path(fake_publish, monkeypatch):
    monkeypatch.setattr(content_router.settings, "celery_enabled", False)
    task = fake_publish(_FakeTask())

    content_router._dispatch([])

    assert task.ran_inline == [] and task.delayed == []


# --------------------------------------------------------------------------- #
# retry_publication                                                            #
# --------------------------------------------------------------------------- #


API = "/api/v1/content"


def test_a_retry_clears_every_trace_of_the_failure_and_dispatches(
    client, auth, content, failed_publication, db, monkeypatch
):
    dispatched: list[int] = []
    monkeypatch.setattr(content_router, "_dispatch", dispatched.extend)

    resp = client.post(
        f"{API}/{content.id}/retry/{failed_publication.id}", headers=auth
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == PublicationStatus.PENDING.value
    assert body["attempts"] == 0
    assert body["error"] is None
    assert dispatched == [failed_publication.id]


def test_a_retry_unparks_a_publication_the_backoff_had_deferred(
    client, auth, content, failed_publication, db, monkeypatch
):
    """A human clicking retry has information the backoff does not.

    Leaving ``scheduled_for`` set would make "retry now" mean "retry whenever
    the exponential backoff had next planned to", which is not what the button
    says.
    """
    from datetime import timedelta

    from app.models.mixins import utcnow

    failed_publication.scheduled_for = utcnow() + timedelta(hours=6)
    db.commit()

    monkeypatch.setattr(content_router, "_dispatch", lambda ids: None)

    resp = client.post(
        f"{API}/{content.id}/retry/{failed_publication.id}", headers=auth
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["scheduled_for"] is None
    db.refresh(failed_publication)
    assert failed_publication.scheduled_for is None


def test_retrying_something_already_live_is_refused(
    client, auth, content, failed_publication, db, monkeypatch
):
    """409, because the retry would post it a second time."""
    failed_publication.status = PublicationStatus.PUBLISHED
    db.commit()

    def fail(ids):  # pragma: no cover - asserted by not being called
        raise AssertionError("a published row must never be dispatched again")

    monkeypatch.setattr(content_router, "_dispatch", fail)

    resp = client.post(
        f"{API}/{content.id}/retry/{failed_publication.id}", headers=auth
    )

    assert resp.status_code == 409
    assert "twice" in resp.json()["detail"]
    db.refresh(failed_publication)
    assert failed_publication.status == PublicationStatus.PUBLISHED


def test_retrying_a_publication_that_does_not_exist_is_a_404(
    client, auth, content, monkeypatch
):
    monkeypatch.setattr(content_router, "_dispatch", lambda ids: None)

    resp = client.post(f"{API}/{content.id}/retry/9999", headers=auth)

    assert resp.status_code == 404


def test_a_publication_belonging_to_another_piece_is_a_404_not_a_retry(
    client, auth, content, failed_publication, db, project, monkeypatch
):
    """The pair has to match, or one piece's id would arm another's publication."""
    other = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Another piece",
        slug="another-piece",
        body_markdown="Body.",
    )
    db.add(other)
    db.commit()

    def fail(ids):  # pragma: no cover - asserted by not being called
        raise AssertionError("a mismatched pair must not dispatch")

    monkeypatch.setattr(content_router, "_dispatch", fail)

    resp = client.post(f"{API}/{other.id}/retry/{failed_publication.id}", headers=auth)

    assert resp.status_code == 404
    db.refresh(failed_publication)
    assert failed_publication.status == PublicationStatus.FAILED


@pytest.mark.parametrize(
    "state", [PublicationStatus.FAILED, PublicationStatus.CANCELLED]
)
def test_a_cancelled_publication_can_be_re_armed_the_same_way(
    client, auth, content, failed_publication, db, monkeypatch, state
):
    """Only PUBLISHED is special; everything else is retryable."""
    failed_publication.status = state
    db.commit()
    dispatched: list[int] = []
    monkeypatch.setattr(content_router, "_dispatch", dispatched.extend)

    resp = client.post(
        f"{API}/{content.id}/retry/{failed_publication.id}", headers=auth
    )

    assert resp.status_code == 200, resp.text
    assert dispatched == [failed_publication.id]
