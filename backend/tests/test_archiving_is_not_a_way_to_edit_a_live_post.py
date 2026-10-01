"""The edit freeze asked the status column, and the column stops saying it first.

``PATCH /content/{id}`` refuses to edit a published piece, and says why: the
edit "would not change what is live on the platforms". Pulse cannot rewrite a
post on Dev.to, so letting its own copy drift from the one people are reading
would leave every screen that renders a title — analytics, the digest, headline
performance — describing a post by a headline that was never on it.

Archiving is the one edit the freeze allows through, and it is documented as
meaning "stop showing me this" rather than "this never went out". But it moves
``status`` to ``archived``, and the guard was reading ``status``. So the refusal
lasted exactly one call:

    PATCH {"body_markdown": "..."}   409 — already published
    PATCH {"status": "archived"}     200 — the allowed edit
    PATCH {"body_markdown": "..."}   200 — and the title, and with it the slug

The post is still up. Nothing about it changed. The stored copy now says
something else, under a different slug than the one its canonical link went out
with — which is the outcome the 409 exists to prevent, reached in two calls
instead of one.

The freeze now asks whether any of the piece actually went out, which is the
question the refusal was always phrased around. Archiving still works, because
``status`` is exempt from the freeze either way.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus

LIVE_URL = "https://dev.to/herald/live-piece"


@pytest.fixture
def live(db, project):
    """A piece that is published here and live on a platform."""
    piece = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Live piece",
        slug="live-piece",
        body_markdown="What the world can read.",
        published_at=utcnow(),
    )
    db.add(piece)
    db.commit()
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow(),
            external_url=LIVE_URL,
        )
    )
    db.commit()
    db.refresh(piece)
    return piece


def _patch(client, auth, piece, **fields):
    return client.patch(f"/api/v1/content/{piece.id}", json=fields, headers=auth)


def test_the_freeze_still_refuses_a_head_on_edit(client, auth, live):
    """The case that always worked, so the fix is not read as loosening it."""
    assert _patch(client, auth, live, body_markdown="rewritten").status_code == 409


def test_archiving_a_published_piece_is_still_allowed(client, auth, live, db):
    """The one edit the freeze lets through has to keep working.

    ``status`` is exempt from the freeze, so widening what counts as frozen
    must not catch the transition that gets a piece out of circulation.
    """
    assert _patch(client, auth, live, status="archived").status_code == 200
    db.refresh(live)
    assert live.status == ContentStatus.ARCHIVED


def test_archiving_does_not_unlock_the_body(client, auth, live, db):
    """The bypass, in the order it was found."""
    assert _patch(client, auth, live, status="archived").status_code == 200

    resp = _patch(client, auth, live, body_markdown="REWRITTEN")

    assert resp.status_code == 409, resp.text
    assert "already published" in resp.json()["detail"].lower()
    db.refresh(live)
    assert live.body_markdown == "What the world can read."


def test_archiving_does_not_unlock_the_title_or_the_slug(client, auth, live, db):
    """The slug is the half that does not come back.

    A title change re-derives the slug, and the slug is what the canonical URL
    on the live post points at. Renaming an archived-but-live piece silently
    moved that target.
    """
    assert _patch(client, auth, live, status="archived").status_code == 200

    resp = _patch(client, auth, live, title="A different headline")

    assert resp.status_code == 409, resp.text
    db.refresh(live)
    assert (live.title, live.slug) == ("Live piece", "live-piece")


def test_a_piece_whose_publish_failed_is_still_editable(client, auth, db, project):
    """The freeze is about what went out, not about having been queued.

    Nothing is live, so there is nothing for an edit to contradict — and this
    is the piece a user most needs to edit, since fixing it is how the retry
    succeeds.
    """
    piece = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.FAILED,
        title="Never made it",
        slug="never-made-it",
        body_markdown="Draft.",
    )
    db.add(piece)
    db.commit()
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.FAILED,
            error="rejected",
        )
    )
    db.commit()

    resp = _patch(client, auth, piece, body_markdown="Fixed.")

    assert resp.status_code == 200, resp.text
    db.refresh(piece)
    assert piece.body_markdown == "Fixed."


def test_a_cancelled_publication_does_not_freeze_the_piece(client, auth, db, project):
    """Cancelling means the piece never went to that platform.

    Same reading as ``sync_content_status``: a cancelled row is not evidence
    of anything, so it cannot be the thing that makes a draft uneditable.
    """
    piece = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="Called off",
        slug="called-off",
        body_markdown="Draft.",
    )
    db.add(piece)
    db.commit()
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.CANCELLED,
        )
    )
    db.commit()

    resp = _patch(client, auth, piece, body_markdown="Rewritten.")

    assert resp.status_code == 200, resp.text


def test_one_live_platform_freezes_a_piece_the_column_calls_archived(
    client, auth, db, project
):
    """The general shape: the column and the platforms can disagree.

    Archived here with one platform live and one cancelled — the freeze follows
    the live row, not the column and not the cancelled one.
    """
    piece = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.ARCHIVED,
        title="Half out",
        slug="half-out",
        body_markdown="Body.",
    )
    db.add(piece)
    db.commit()
    for platform, state in (
        (Platform.DEVTO, PublicationStatus.PUBLISHED),
        (Platform.MEDIUM, PublicationStatus.CANCELLED),
    ):
        db.add(
            Publication(content_id=piece.id, platform=platform, status=state)
        )
    db.commit()

    assert _patch(client, auth, piece, body_markdown="nope").status_code == 409
    # And the way out of circulation is still open.
    assert _patch(client, auth, piece, status="archived").status_code == 200


def test_the_freeze_costs_no_extra_query(client, auth, live, sql_log):
    """``_is_live`` reads a relationship the request has already loaded.

    Asserted because the obvious implementation of "did any of this go out" is
    a COUNT against ``publications``, on the endpoint every editor keystroke
    eventually reaches.
    """
    before = len(sql_log)
    assert _patch(client, auth, live, body_markdown="rewritten").status_code == 409
    selects = [s for s in sql_log[before:] if s.lower().startswith("select")]

    # The piece, its project (ownership), and its publications. Nothing more.
    assert len(selects) <= 3, selects
