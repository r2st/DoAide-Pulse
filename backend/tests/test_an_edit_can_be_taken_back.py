"""``Content.version`` counted writes; now something remembers what they said.

The counter has been a real ``version_id_col`` since the two-editors work: it
increments on every write and the PATCH route hands it back as an ``ETag``, so a
save landing on top of somebody else's is refused with a 412. What it could
never do is show you the paragraph you lost — the text that was version 4 was
gone the moment version 5 was written, which made the conflict machinery a
strictly worse deal than it looked.

This file covers the history that fixes that, and the three properties that make
it worth having rather than a table that grows:

* a snapshot is taken **before** the write and only when the write changes
  something, so the history is the text as it was and not a log of autosaves;
* a restore is **another edit** — it banks what it replaces, so the button is
  never the destructive one;
* a restore moves the **seven editable fields and nothing else**, so it cannot
  un-publish a piece or re-arm a queue that has already run.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.revision import ContentRevision, RevisionSource
from app.services import revisions

V1 = "/api/v1"


@pytest.fixture
def piece(db, project) -> Content:
    """A draft with something in every field a revision captures."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        title="The first headline",
        slug="the-first-headline",
        body_markdown="# The first headline\n\nA paragraph that was here first.\n",
        excerpt="The excerpt as first written.",
        meta_description="A meta description, as first written.",
        keywords=["publishing", "automation"],
        tags=["pulse", "release"],
        focus_keyword="publishing",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _patch(client, auth, piece, **fields):
    return client.patch(f"{V1}/content/{piece.id}", json=fields, headers=auth)


# --------------------------------------------------------------------------- #
# When a snapshot is taken                                                      #
# --------------------------------------------------------------------------- #


def test_an_edit_banks_the_text_it_replaces(client, auth, db, piece):
    """Revision *n* holds the text as of version *n* — before the write, not after.

    The direction is the whole point. A snapshot taken afterwards stores the new
    text under the old number, which is the one arrangement that makes a history
    actively misleading: every entry would show the change that came after it.
    """
    original_version = piece.version
    original_body = piece.body_markdown

    response = _patch(client, auth, piece, body_markdown="# New\n\nRewritten.\n")
    assert response.status_code == 200

    stored = db.scalars(
        db.query(ContentRevision).filter_by(content_id=piece.id).statement
    ).all()
    assert len(stored) == 1
    assert stored[0].revision == original_version
    assert stored[0].body_markdown == original_body
    assert stored[0].source == RevisionSource.EDIT

    db.refresh(piece)
    assert piece.version == original_version + 1


def test_a_patch_that_changes_nothing_writes_no_revision(client, auth, db, piece):
    """The editor autosaves on a timer, not on a keystroke.

    Most PATCHes re-send a body identical to the stored one. A history where
    nine entries in ten say "no changes" is a log with the useful rows hidden
    in it, so an unchanged write is not an entry.
    """
    _patch(client, auth, piece, body_markdown=piece.body_markdown, title=piece.title)

    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 0


def test_a_status_only_change_writes_no_revision(client, auth, db, piece):
    """Only the seven editable fields count as text.

    Moving a piece to review or archiving it has not touched a word of it, and
    an entry that renders as "no changes" is noise in the one list a user reads
    to find out what happened to their subheading.
    """
    _patch(client, auth, piece, status=ContentStatus.ARCHIVED.value)

    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 0


def test_the_note_names_the_fields_that_moved(client, auth, db, piece):
    """What turns a column of timestamps into something a user can scan."""
    _patch(client, auth, piece, title="A second headline", tags=["pulse", "changelog"])

    stored = db.query(ContentRevision).filter_by(content_id=piece.id).one()
    assert "title" in stored.note
    assert "tags" in stored.note
    assert "body_markdown" not in stored.note


def test_a_failed_edit_leaves_no_history_entry(client, auth, db, piece):
    """The snapshot joins the request's transaction rather than committing early.

    A PATCH refused by the review-readiness gate never happened, and a history
    entry for it would point at a version that does not exist. That gate runs
    *after* the snapshot — it has to, because it scores the piece as the request
    would leave it — and rolls back explicitly, so this is the case that proves
    the snapshot is not committed on its own.
    """
    all_code = "```python\n" + "\n".join(f"x{n} = compute({n})" for n in range(60)) + "\n```\n"

    response = client.patch(
        f"{V1}/content/{piece.id}",
        json={"body_markdown": all_code, "status": ContentStatus.REVIEW.value},
        headers=auth,
    )
    assert response.status_code == 409
    assert "of the body is code" in response.json()["detail"]

    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 0
    db.refresh(piece)
    assert piece.body_markdown.startswith("# The first headline")


# --------------------------------------------------------------------------- #
# Reading the history                                                           #
# --------------------------------------------------------------------------- #


def test_the_history_is_newest_first_and_carries_no_bodies(client, auth, piece):
    """A sidebar of timestamps must not download every version to render itself."""
    _patch(client, auth, piece, body_markdown="# One\n\nFirst rewrite.\n")
    _patch(client, auth, piece, body_markdown="# Two\n\nSecond rewrite.\n")

    response = client.get(f"{V1}/content/{piece.id}/revisions", headers=auth)
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert [item["revision"] for item in body["items"]] == sorted(
        (item["revision"] for item in body["items"]), reverse=True
    )
    assert "body_markdown" not in body["items"][0]
    assert body["items"][0]["word_count"] > 0
    assert response.headers["X-Total-Count"] == "2"


def test_one_revision_comes_back_whole(client, auth, piece):
    """The detail endpoint is where the text lives."""
    original = piece.body_markdown
    version = piece.version
    _patch(client, auth, piece, body_markdown="# Replaced\n\nEntirely.\n")

    response = client.get(f"{V1}/content/{piece.id}/revisions/{version}", headers=auth)
    assert response.status_code == 200
    assert response.json()["body_markdown"] == original


def test_a_piece_that_was_never_edited_has_no_history(client, auth, piece):
    """No synthetic entry for the current text: there is nothing to go back to."""
    response = client.get(f"{V1}/content/{piece.id}/revisions", headers=auth)

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_asking_for_a_version_that_is_not_stored_says_what_is(client, auth, piece):
    """A 404 that names the current version and the retention depth.

    "No version 3" is a dead end; "no version 3, this piece is at version 2, and
    older versions are kept 50 deep" tells the user which of the two possible
    causes they hit.
    """
    response = client.get(f"{V1}/content/{piece.id}/revisions/99", headers=auth)

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "99" in detail
    assert str(revisions.RETENTION) in detail


# --------------------------------------------------------------------------- #
# The diff                                                                      #
# --------------------------------------------------------------------------- #


def test_the_diff_reports_only_the_fields_that_changed(client, auth, piece):
    """Seven fields of which one differs buries the answer in six blanks."""
    version = piece.version
    _patch(client, auth, piece, title="A different headline")

    response = client.get(
        f"{V1}/content/{piece.id}/revisions/{version}/diff", headers=auth
    )
    assert response.status_code == 200
    fields = {item["field"] for item in response.json()["fields"]}
    assert fields == {"title"}


def test_a_body_diff_comes_back_as_unified_lines(client, auth, piece):
    """Bodies have lines; a focus keyword does not, and gets before/after."""
    version = piece.version
    _patch(
        client,
        auth,
        piece,
        body_markdown="# The first headline\n\nA paragraph that is different now.\n",
        focus_keyword="automation",
    )

    response = client.get(
        f"{V1}/content/{piece.id}/revisions/{version}/diff", headers=auth
    )
    by_field = {item["field"]: item for item in response.json()["fields"]}

    assert any(line.startswith("-") for line in by_field["body_markdown"]["unified"])
    assert any(line.startswith("+") for line in by_field["body_markdown"]["unified"])
    assert by_field["focus_keyword"]["unified"] == []
    assert by_field["focus_keyword"]["before"] == "publishing"
    assert by_field["focus_keyword"]["after"] == "automation"


def test_two_stored_revisions_can_be_compared_with_each_other(client, auth, piece):
    """``?against=`` compares two versions rather than one against the present."""
    first = piece.version
    _patch(client, auth, piece, title="Second headline")
    second = first + 1
    _patch(client, auth, piece, title="Third headline")

    response = client.get(
        f"{V1}/content/{piece.id}/revisions/{first}/diff",
        params={"against": second},
        headers=auth,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["to_revision"] == second
    title = next(item for item in body["fields"] if item["field"] == "title")
    assert title["before"] == "The first headline"
    assert title["after"] == "Second headline"


def test_a_list_field_diffs_as_a_person_reads_it(client, auth, piece):
    """``tags`` is JSON; reordering it is a change, because the first tag leads."""
    version = piece.version
    _patch(client, auth, piece, tags=["release", "pulse"])

    response = client.get(
        f"{V1}/content/{piece.id}/revisions/{version}/diff", headers=auth
    )
    tags = next(item for item in response.json()["fields"] if item["field"] == "tags")
    assert tags["before"] == "pulse, release"
    assert tags["after"] == "release, pulse"


# --------------------------------------------------------------------------- #
# Restoring                                                                     #
# --------------------------------------------------------------------------- #


def test_a_restore_puts_the_old_text_back(client, auth, db, piece):
    """The round trip, on every field a revision captures."""
    version = piece.version
    original = {
        "title": piece.title,
        "body_markdown": piece.body_markdown,
        "excerpt": piece.excerpt,
        "meta_description": piece.meta_description,
        "keywords": list(piece.keywords),
        "tags": list(piece.tags),
        "focus_keyword": piece.focus_keyword,
    }
    _patch(
        client,
        auth,
        piece,
        title="Something else entirely",
        body_markdown="# Else\n\nDifferent.\n",
        excerpt="Different excerpt.",
        meta_description="Different meta.",
        keywords=["other"],
        tags=["other"],
        focus_keyword="other",
    )

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth
    )
    assert response.status_code == 200

    db.refresh(piece)
    for field, value in original.items():
        assert getattr(piece, field) == value, field


def test_a_restore_is_itself_reversible(client, auth, db, piece):
    """The text a restore replaces is banked, so undoing a restore is one click.

    A restore that simply overwrote would be the only irreversible operation in
    a feature whose entire purpose is reversibility.
    """
    first_version = piece.version
    _patch(client, auth, piece, title="The rewritten headline")

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{first_version}/restore", headers=auth
    )
    body = response.json()
    assert body["restored_revision"] == first_version
    previous = body["previous_revision"]

    db.refresh(piece)
    assert piece.title == "The first headline"

    # And back again, using the number the restore handed back.
    client.post(f"{V1}/content/{piece.id}/revisions/{previous}/restore", headers=auth)
    db.refresh(piece)
    assert piece.title == "The rewritten headline"


def test_a_restore_returns_the_new_version_and_etag(client, auth, db, piece):
    """Without it the editor's next save carries a stale ``If-Match`` and 412s."""
    version = piece.version
    _patch(client, auth, piece, title="Rewritten")

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth
    )

    db.refresh(piece)
    assert response.json()["version"] == piece.version
    assert response.headers["ETag"] == f'"{piece.version}"'

    # And the returned version is one the API will actually accept.
    accepted = client.patch(
        f"{V1}/content/{piece.id}",
        json={"title": "After the restore"},
        headers={**auth, "If-Match": response.headers["ETag"]},
    )
    assert accepted.status_code == 200


def test_a_restore_moves_the_title_and_the_slug_with_it(client, auth, db, piece):
    """The slug follows the title through the same helper the PATCH route uses.

    Recomputed rather than restored: a stored slug may since have been taken by
    another piece in the project, and the unique index would answer that with an
    IntegrityError at commit rather than with a suffix.
    """
    version = piece.version
    _patch(client, auth, piece, title="A completely different headline")
    db.refresh(piece)
    assert piece.slug == "a-completely-different-headline"

    client.post(f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth)

    db.refresh(piece)
    assert piece.title == "The first headline"
    assert piece.slug == "the-first-headline"


def test_a_restore_does_not_touch_the_queue(client, auth, db, piece):
    """Only the seven editable fields move.

    A restore must not re-arm a publication that has already run. Keeping status
    and the publication rows out of the snapshot is what makes that structural
    rather than a rule somebody has to remember.
    """
    version = piece.version
    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_url="https://dev.to/x/already-out",
    )
    db.add(publication)
    db.commit()

    _patch(client, auth, piece, title="Rewritten while a copy is out")
    client.post(f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth)

    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.external_url == "https://dev.to/x/already-out"


def test_a_published_piece_refuses_a_restore(client, auth, db, piece):
    """The same refusal, in the same words, that PATCH gives for the same reason.

    Changing the text here would make Pulse disagree with what a reader can see
    without changing anything a reader can see.
    """
    version = piece.version
    _patch(client, auth, piece, title="Rewritten")
    piece.status = ContentStatus.PUBLISHED
    db.commit()

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth
    )

    assert response.status_code == 409
    assert "already published" in response.json()["detail"].lower()


def test_an_archived_piece_that_went_out_refuses_a_restore_too(
    client, auth, db, piece
):
    """The freeze follows the rows, not the column. Archiving a published
    piece and restoring an old version of it would rewrite Pulse's copy of a
    post that is still up — the two-call edit the PATCH's freeze is closed
    against, reached through the history sidebar instead."""
    version = piece.version
    _patch(client, auth, piece, title="Rewritten")
    piece.status = ContentStatus.ARCHIVED
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            external_url="https://dev.to/x/still-up",
        )
    )
    db.commit()

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth
    )

    assert response.status_code == 409
    assert "already published" in response.json()["detail"].lower()
    db.refresh(piece)
    assert piece.title == "Rewritten"


def test_an_archived_piece_still_restores(client, auth, db, piece):
    """Archiving means "stop showing me this", not "this went out"."""
    version = piece.version
    _patch(client, auth, piece, title="Rewritten")
    piece.status = ContentStatus.ARCHIVED
    db.commit()

    response = client.post(
        f"{V1}/content/{piece.id}/revisions/{version}/restore", headers=auth
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Retention                                                                     #
# --------------------------------------------------------------------------- #


def test_the_history_is_bounded_and_drops_the_oldest_first(db, piece, monkeypatch):
    """Unbounded history is the version of this that works for a year.

    A piece under an hourly automated headline test writes a revision an hour
    forever, and the bodies are kilobytes each. The bound is applied at write
    time rather than by a nightly sweep because it is per piece, and the write
    already has the piece in hand.
    """
    monkeypatch.setattr(revisions, "RETENTION", 5)

    for index in range(12):
        piece.body_markdown = f"# Draft {index}\n\nRevision number {index}.\n"
        revisions.snapshot(db, piece)
        db.commit()
        db.refresh(piece)

    stored = (
        db.query(ContentRevision)
        .filter_by(content_id=piece.id)
        .order_by(ContentRevision.revision)
        .all()
    )
    assert len(stored) == 5
    # The five most recent, and the oldest is gone rather than the newest.
    assert [row.revision for row in stored] == sorted(row.revision for row in stored)
    assert stored[-1].revision > stored[0].revision


def test_two_snapshots_of_one_version_are_one_row(db, piece):
    """Idempotent per version: the pair (piece, version) names one text.

    Two code paths in one request both deciding to be careful must not trip the
    unique constraint — and must not write two rows that disagree.
    """
    first = revisions.snapshot(db, piece)
    second = revisions.snapshot(db, piece)

    assert first is second
    db.commit()
    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 1


def test_deleting_a_piece_takes_its_history_with_it(client, auth, db, piece):
    """A history of a deleted piece is not something anybody can act on."""
    _patch(client, auth, piece, title="Rewritten")
    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 1

    assert client.delete(f"{V1}/content/{piece.id}", headers=auth).status_code == 204

    assert db.query(ContentRevision).filter_by(content_id=piece.id).count() == 0


def test_the_snapshot_does_not_alias_the_list_it_copies(db, piece):
    """A ``tags`` list handed straight to the revision is the *same object*.

    The PATCH's ``setattr`` loop would then mutate the snapshot it just took,
    and because SQLAlchemy's JSON columns compare by value on flush, the
    corruption would be written out with no error anywhere.
    """
    stored = revisions.snapshot(db, piece)
    piece.tags.append("mutated-after-the-snapshot")
    db.commit()

    assert "mutated-after-the-snapshot" not in stored.tags
