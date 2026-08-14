"""``PATCH /content/{id}`` was last-write-wins, and said nothing about it.

Two people with the same piece open — or one person with two tabs, or a tab and
the autopilot — each loaded the row, each edited a different paragraph, and each
saved. The second save wrote every field it was holding, including the ones it
had read before the first save changed them. One of the two edits was gone, the
API had answered 200 to both, and nothing anywhere recorded that a paragraph had
been written over. The editor auto-saves two seconds after the last keystroke,
so this is not an exotic race: it is what two people editing the same draft on a
Tuesday afternoon produce.

The fix is a ``version`` column and two layers over it.

*The database layer.* ``Content`` now maps ``version_id_col``, so every UPDATE
SQLAlchemy emits for the row carries ``AND version = <the one we loaded>``. Two
transactions that overlap cannot both win: the loser matches no row and raises
``StaleDataError``, which :func:`app.main.create_app` turns into a 409 rather
than the 500 an unhandled one would be.

*The HTTP layer.* Two humans in two tabs almost never overlap in the database
sense — one saves at 12:00:01 and the other at 12:00:09, and each transaction is
milliseconds long. So the version is also published, in the body and in an
``ETag``, and a client may send it back as ``If-Match``. A save whose ``If-Match``
names a version the piece has moved past is refused with a 412 that says who is
where. Without the header the endpoint behaves exactly as it did, which is what
keeps every existing client working.

The two layers catch different things and neither subsumes the other, so both
are pinned here.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.DRAFT,
        title="A shared draft",
        slug="a-shared-draft",
        body_markdown="The first paragraph.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _patch(client, auth, piece, *, if_match=None, **fields):
    headers = dict(auth)
    if if_match is not None:
        headers["If-Match"] = if_match
    return client.patch(f"/api/v1/content/{piece.id}", json=fields, headers=headers)


# --------------------------------------------------------------------------- #
# The version is published                                                     #
# --------------------------------------------------------------------------- #


def test_a_new_piece_starts_at_version_one(client, auth, piece):
    body = client.get(f"/api/v1/content/{piece.id}", headers=auth).json()
    assert body["version"] == 1


def test_the_detail_response_carries_the_version_as_an_etag(client, auth, piece):
    resp = client.get(f"/api/v1/content/{piece.id}", headers=auth)
    # Quoted: an entity tag is `DQUOTE *etagc DQUOTE`, and a bare 1 is not one.
    assert resp.headers["ETag"] == '"1"'


def test_the_listing_carries_the_version_too(client, auth, piece):
    """So a client can hold a precondition without fetching each piece first."""
    rows = client.get("/api/v1/content", headers=auth).json()
    assert [row["version"] for row in rows] == [1]


def test_a_successful_save_bumps_the_version_and_returns_the_new_one(
    client, auth, piece
):
    resp = _patch(client, auth, piece, title="Edited once")
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    # The header agrees with the body, so a client may use either.
    assert resp.headers["ETag"] == '"2"'

    assert _patch(client, auth, piece, title="Edited twice").json()["version"] == 3


def test_a_save_that_changes_nothing_does_not_bump_the_version(client, auth, piece):
    """No UPDATE, no bump. SQLAlchemy only emits one for a real change, and a
    version that moved on every request would expire preconditions that nothing
    had actually invalidated — including the client's own re-save of the same
    text, which the editor's auto-save does whenever a keystroke is undone."""
    assert _patch(client, auth, piece, title=piece.title).json()["version"] == 1


def test_the_version_is_not_something_a_client_may_set(client, auth, piece):
    """``ContentUpdate`` has no ``version`` field, so sending one is ignored
    rather than obeyed — the count is the database's, not the caller's."""
    assert _patch(client, auth, piece, version=99, title="Nice try").json()["version"] == 2


# --------------------------------------------------------------------------- #
# If-Match                                                                     #
# --------------------------------------------------------------------------- #


def test_a_save_with_the_current_version_goes_through(client, auth, piece):
    resp = _patch(client, auth, piece, if_match='"1"', body_markdown="Mine.")
    assert resp.status_code == 200
    assert resp.json()["body_markdown"] == "Mine."


def test_the_second_of_two_editors_is_refused_rather_than_obeyed(client, auth, piece):
    """The whole point, in the shape it actually happens in.

    Both tabs loaded version 1. The first saves and the piece becomes version 2.
    The second saves the text it read *before* that, and is told.
    """
    first_tab_version = client.get(
        f"/api/v1/content/{piece.id}", headers=auth
    ).json()["version"]
    second_tab_version = first_tab_version

    assert _patch(
        client, auth, piece, if_match=f'"{first_tab_version}"',
        body_markdown="The first editor's paragraph.",
    ).status_code == 200

    stale = _patch(
        client, auth, piece, if_match=f'"{second_tab_version}"',
        body_markdown="The second editor's paragraph.",
    )
    assert stale.status_code == 412
    assert "version 1" in stale.json()["detail"]
    assert "it is now 2" in stale.json()["detail"]


def test_a_refused_save_leaves_the_piece_exactly_as_it_was(client, auth, db, piece):
    _patch(client, auth, piece, if_match='"1"', body_markdown="Kept.")
    _patch(client, auth, piece, if_match='"1"', body_markdown="Discarded.", title="Also discarded")

    db.refresh(piece)
    assert piece.body_markdown == "Kept."
    assert piece.title == "A shared draft"
    assert piece.version == 2


def test_a_star_matches_whatever_is_there(client, auth, piece):
    """RFC 9110 §13.1.1: ``*`` is "any current representation". The piece was
    loaded before the check ran, so one exists by construction."""
    assert _patch(client, auth, piece, if_match="*", title="Whatever").status_code == 200


def test_any_tag_in_the_list_matching_is_a_match(client, auth, piece):
    resp = _patch(client, auth, piece, if_match='"9", "1" ,"7"', title="One of these")
    assert resp.status_code == 200


def test_a_list_with_no_current_tag_in_it_is_refused(client, auth, piece):
    resp = _patch(client, auth, piece, if_match='"9","7"', title="None of these")
    assert resp.status_code == 412
    # Both are quoted back, sorted, so the client can see what it sent.
    assert "version 7/9" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("header", "why"),
    [
        ("1", "unquoted"),
        ('W/"1"', "weak — If-Match requires a strong comparison"),
        ('"', "one character, not a pair of quotes round anything"),
        ('"1', "unterminated"),
    ],
)
def test_a_header_that_is_not_an_entity_tag_is_a_400_not_a_silent_pass(
    client, auth, piece, header, why
):
    """The request asked for a guarantee in a spelling this server does not
    parse. Answering 200 would claim the guarantee was checked."""
    resp = _patch(client, auth, piece, if_match=header, title="Should not land")
    assert resp.status_code == 400, why
    assert "quoted entity tag" in resp.json()["detail"]
    assert client.get(f"/api/v1/content/{piece.id}", headers=auth).json()["version"] == 1


def test_an_empty_if_match_is_a_400(client, auth, piece):
    resp = _patch(client, auth, piece, if_match="  ", title="Should not land")
    assert resp.status_code == 400
    assert "sent empty" in resp.json()["detail"]


def test_no_header_still_saves_unconditionally(client, auth, piece):
    """Every client written before the field existed keeps working."""
    assert _patch(client, auth, piece, title="No precondition").status_code == 200


# --------------------------------------------------------------------------- #
# Order of the refusals                                                        #
# --------------------------------------------------------------------------- #


def test_a_stale_precondition_beats_the_published_freeze(client, auth, db, project):
    """A 409 saying "already published" sends the client to look at the piece's
    state. A 412 sends it to reload. When both are true the second is the useful
    advice, because reloading is what reveals the first."""
    live = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        title="Live",
        slug="live",
        body_markdown="Out in the world.",
        published_at=utcnow(),
    )
    db.add(live)
    db.commit()
    db.add(
        Publication(
            content_id=live.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow(),
        )
    )
    db.commit()

    resp = _patch(client, auth, live, if_match='"999"', body_markdown="Rewritten")
    assert resp.status_code == 412


def test_a_stale_precondition_beats_the_null_field_check(client, auth, piece):
    """Same reason: which piece you are holding is a question that comes before
    what you are asking to write to it."""
    resp = _patch(client, auth, piece, if_match='"999"', title=None)
    assert resp.status_code == 412


# --------------------------------------------------------------------------- #
# Everything that writes a piece moves the version                             #
# --------------------------------------------------------------------------- #


def test_an_edit_through_another_endpoint_expires_the_editors_precondition(
    client, auth, piece
):
    """The version is the mapper's, not the PATCH endpoint's.

    Applying a headline is a different route with a different body, and an
    editor holding version 1 must not be able to write over it. Nothing in
    ``apply_content_headline`` knows the version exists — that is what makes the
    guarantee hold for the route nobody has written yet.
    """
    applied = client.post(
        f"/api/v1/content/{piece.id}/headlines/apply",
        json={"title": "A better headline"},
        headers=auth,
    )
    assert applied.status_code == 200

    stale = _patch(client, auth, piece, if_match='"1"', body_markdown="From the editor")
    assert stale.status_code == 412


# --------------------------------------------------------------------------- #
# The database layer, for writers whose transactions genuinely overlap         #
# --------------------------------------------------------------------------- #


def test_a_write_against_a_version_the_row_has_moved_past_is_refused(db, piece):
    """The lower layer, without HTTP.

    ``set_committed_value`` is how a concurrent commit is staged here: the suite
    runs on one shared SQLite connection, so a second session is not available
    to genuinely race, and what matters is the state it would leave behind — an
    instance whose loaded version no longer matches the row's. The UPDATE then
    matches nothing, which is exactly what the losing writer sees.
    """
    from sqlalchemy.orm.exc import StaleDataError

    set_committed_value(piece, "version", 41)
    piece.title = "Written from a stale copy"
    with pytest.raises(StaleDataError):
        db.commit()
    db.rollback()


def test_the_losing_writer_gets_a_409_rather_than_a_500(client, auth, piece):
    """And the API says something a person can act on.

    The route loads the piece from this same session, so tampering with the
    identity-mapped instance is what the route will find — the request commits
    against a version the row does not have, and unwinds through the handler in
    :func:`app.main.create_app`.
    """
    set_committed_value(piece, "version", 41)

    resp = _patch(client, auth, piece, title="Written from a stale copy")
    assert resp.status_code == 409
    assert "Reload and try again" in resp.json()["detail"]
    # Not the catch-all's body, which tells the caller nothing but a request id.
    assert resp.json()["detail"] != "Internal server error"


def test_deleting_from_a_stale_copy_is_refused_too(client, auth, piece):
    """``version_id_col`` puts the predicate on DELETE as well as UPDATE, which
    is what stops a delete from landing on a row somebody has since edited."""
    set_committed_value(piece, "version", 41)

    resp = client.delete(f"/api/v1/content/{piece.id}", headers=auth)
    assert resp.status_code == 409
