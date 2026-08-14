"""An autopilot destination nobody connected must not cost the piece.

``_publishable_destinations`` already drops two kinds of destination before
anything is queued, and its docstring says why: an unknown platform string would
raise out of a beat sweep, and a platform with no finished adapter "fails the
publication terminally, which drives the piece to ``failed`` instead of leaving
it approved".

A platform the owner has never connected is the third kind, and it was not
dropped. It fails for a different reason and with the identical consequence:
``_credentials_for`` raises ``NotConnected``, ``execute`` treats it as terminal —
correctly, since no amount of retrying connects an account — and
``_sync_content_status`` then walks a piece whose every publication is terminal
to ``failed``.

So a project set to ``auto`` with one destination it had never connected did
this, every hour, and the observed end state was a ``failed`` piece:

    write -> approve -> queue -> NotConnected -> terminal -> content failed

The work was thrown away for a missing credential, with nothing to be done but
connect the account and retry by hand. This is the one production symptom the
box actually showed: a FAILED Bluesky publication on a project whose
``autopilot_platforms`` named ``bluesky`` and whose owner had only ever
connected Dev.to.

The manual publish path never had this problem — ``_queue_publish`` refuses an
unconnected platform with a 400 that names it. The autopilot has nobody to show
a 400 to, so it drops the destination, records it on the piece where the other
gate results already go, and lets the piece reach a human intact.
"""
from __future__ import annotations

import json

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.models.publication import Platform, PublicationStatus
from app.services import content_pipeline, llm_router, publishing_service


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(content_pipeline, "publish_now", lambda pid: None)


@pytest.fixture
def confident(monkeypatch):
    """A generation good enough to clear every other gate."""

    def fake_complete(messages, **kwargs):
        return llm_router.Completion(
            text=json.dumps(
                {
                    "title": "Herald ships marketing automation",
                    "body_markdown": "## Why\n\n" + ("word " * 300),
                    "excerpt": "Herald writes the posts about what you ship.",
                    "meta_description": "Herald automates developer marketing "
                    "end to end, from repo watch to published post.",
                    "keywords": ["marketing automation"],
                    "tags": ["python"],
                    "confidence": 0.95,
                }
            ),
            provider="stub",
            model="stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    monkeypatch.setattr(content_pipeline.seo, "SEO_SCORE_THRESHOLD", 0)
    monkeypatch.setattr(content_pipeline.settings, "link_check_enabled", False)


def _route(db, project):
    return content_pipeline.generate_and_route(
        db, project, content_type=ContentType.ANNOUNCEMENT, source={"kind": "test"}
    )


@pytest.fixture
def auto_project(db, project):
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["bluesky"]
    db.commit()
    return project


# ---- The destination itself ---------------------------------------------- #


def test_an_unconnected_destination_is_not_publishable(db, auto_project):
    found = content_pipeline._publishable_destinations(auto_project)

    assert found.usable == []
    assert found.unconnected == ["bluesky"]


def test_a_connected_destination_still_is(db, auto_project, connect):
    connect("bluesky")

    found = content_pipeline._publishable_destinations(auto_project)

    assert found.usable == [Platform.BLUESKY]
    assert found.unconnected == []


def test_only_the_unconnected_half_is_dropped(db, project, connect):
    """A cross-post to one connected and one unconnected platform still goes out
    on the one that works."""
    connect("devto")
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto", "bluesky"]
    db.commit()

    found = content_pipeline._publishable_destinations(project)

    assert found.usable == [Platform.DEVTO]
    assert found.unconnected == ["bluesky"]


def test_a_platform_named_twice_is_only_reported_once(db, project):
    """``autopilot_platforms`` is a plain JSON list and nothing dedupes it on
    read — the reason ``usable`` dedupes too. A reviewer told a piece could not
    reach "bluesky, bluesky" learns nothing the first one did not say.
    """
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["bluesky", "bluesky"]
    db.commit()

    assert content_pipeline._publishable_destinations(project).unconnected == ["bluesky"]


def test_a_connection_that_went_invalid_is_not_a_connection(db, auto_project, connect):
    """``connected_platforms`` reads the status, not the row's existence.

    A revoked token leaves the row behind with ``status=invalid`` — set by
    ``_mark_connection_invalid`` the first time the platform rejected it — and
    publishing to it would fail exactly as if it had never been added.
    """
    from app.models.platform_connection import ConnectionStatus, PlatformConnection

    connect("bluesky")
    row = db.query(PlatformConnection).one()
    row.status = ConnectionStatus.INVALID
    db.commit()

    assert content_pipeline._publishable_destinations(auto_project).usable == []


# ---- What that means for the piece ---------------------------------------- #


def test_the_piece_goes_to_review_rather_than_being_published_and_burned(
    db, auto_project, confident
):
    """The regression this file exists for, end to end."""
    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status is ContentStatus.REVIEW
    assert routed.content.publications == [], "nothing was queued to fail"
    assert routed.platforms == []


def test_the_reviewer_is_told_why_it_is_in_front_of_them(db, auto_project, confident):
    """Alongside the other gate results, which is where a reviewer looks."""
    routed = _route(db, auto_project)

    assert routed.content.source["unconnected_platforms"] == ["bluesky"]


def test_a_piece_that_could_publish_records_nothing_to_explain(
    db, auto_project, confident, connect
):
    """The field is empty when there was nothing to drop, not absent."""
    connect("bluesky")

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.content.source["unconnected_platforms"] == []


def test_the_old_behaviour_would_have_failed_the_piece(
    db, auto_project, confident, connect, monkeypatch
):
    """The cost of the bug, stated as an assertion rather than as a story.

    Queueing the unconnected destination anyway — which is what dropping the
    check restores — publishes nothing and leaves the piece in ``failed``.
    """
    monkeypatch.setattr(
        content_pipeline,
        "_publishable_destinations",
        lambda project: content_pipeline._Destinations(
            usable=[Platform.BLUESKY], unconnected=[]
        ),
    )

    routed = _route(db, auto_project)
    (publication,) = routed.content.publications
    publishing_service.execute(db, publication)

    db.refresh(publication)
    db.refresh(routed.content)
    assert publication.status is PublicationStatus.FAILED
    assert "No live bluesky connection" in (publication.error or "")
    assert routed.content.status is ContentStatus.FAILED


# ---- The approval path takes the same view -------------------------------- #


def test_approving_queues_nothing_for_an_unconnected_project(db, auto_project):
    """``release_approved`` shares the check, so a human clicking Approve on an
    ``auto`` project with no connected destination does not burn the piece
    either. It stays approved, and the sweep releases it once the account is
    connected.
    """
    content = Content(
        project_id=auto_project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="word " * 200,
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    assert content_pipeline.release_approved(db, content) == []
    db.refresh(content)
    assert content.status is ContentStatus.APPROVED


def test_connecting_the_account_later_releases_the_waiting_piece(
    db, auto_project, connect
):
    """Which is what makes leaving it ``approved`` the right answer rather than
    a different way to lose it."""
    content = Content(
        project_id=auto_project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="word " * 200,
        status=ContentStatus.APPROVED,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    assert content_pipeline.release_approved(db, content) == []

    connect("bluesky")

    published = content_pipeline.release_approved(db, content)

    assert [p.platform for p in published] == [Platform.BLUESKY]
