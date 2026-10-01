"""The three endpoints whose effect cannot be inspected afterwards.

Pulse archives, cancels and revokes rather than deleting nearly everywhere, and
those states are all readable back off the row. Three paths genuinely destroy:

* ``DELETE /projects/{id}`` cascades to the content, the publication history and
  the triggers. Its own docstring says it — "this one really deletes" — and adds
  the part that makes it worth a log line: *content already live on a platform
  stays live*. Afterwards there is nothing left to ask what went, and the posts
  it was the only record of are still out there under Pulse's name.
* ``DELETE /content/{id}`` is the same shape one level down.
* ``DELETE /settings/connections/{platform}`` destroys the ciphertext on
  purpose, and takes with it the ability to publish anything queued for that
  platform — which is the cause behind a whole class of "No live connection"
  publication failures, arriving minutes or days later with nothing linking the
  two.

None of the three wrote anything anywhere. A deletion is the one event where
"look at the row" is not available as a fallback, so the log line is not a
convenience — it is the only record there will ever be. The counts are the
substance of it: "project 11 was deleted" and "project 11 was deleted and took
twenty publications of live posts with it" are different facts, and only the
second is worth keeping.
"""
from __future__ import annotations

import logging

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus

V1 = "/api/v1"


def _piece(db, project, *, slug: str) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title=slug.replace("-", " ").title(),
        slug=slug,
        status=ContentStatus.PUBLISHED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(db, piece, platform, *, status=PublicationStatus.PUBLISHED) -> Publication:
    row = Publication(
        content_id=piece.id,
        platform=platform,
        status=status,
        external_id=f"ext-{piece.id}-{platform.value}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _lines(caplog, logger: str, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == logger and r.levelno == level
    ]


# --------------------------------------------------------------------------- #
# Projects                                                                     #
# --------------------------------------------------------------------------- #


def test_deleting_a_project_records_what_the_cascade_took(
    client, db, auth, user, project, caplog
):
    first = _piece(db, project, slug="one")
    second = _piece(db, project, slug="two")
    _publication(db, first, Platform.DEVTO)
    _publication(db, first, Platform.HASHNODE)
    _publication(db, second, Platform.DEVTO)

    with caplog.at_level(logging.WARNING, logger="app.routers.projects"):
        resp = client.delete(f"{V1}/projects/{project.id}", headers=auth)
    assert resp.status_code == 204

    lines = _lines(caplog, "app.routers.projects", logging.WARNING)
    assert len(lines) == 1
    assert f"project {project.id} ({project.slug}) deleted" in lines[0]
    assert f"by user {user.id}" in lines[0]
    assert "2 piece(s)" in lines[0]
    assert "3 publication(s)" in lines[0]
    # And the row really is gone, so the line is all there is.
    assert db.get(Project, project.id) is None


def test_an_empty_project_says_it_took_nothing(client, db, auth, project, caplog):
    """The counts have to be real, or the line cannot be read as an audit record."""
    with caplog.at_level(logging.WARNING, logger="app.routers.projects"):
        assert client.delete(f"{V1}/projects/{project.id}", headers=auth).status_code == 204

    line = _lines(caplog, "app.routers.projects", logging.WARNING)[0]
    assert "0 piece(s)" in line
    assert "0 publication(s)" in line


def test_the_count_is_this_projects_own(client, db, auth, user, project, caplog):
    """A second project's rows must not be attributed to the one being deleted."""
    other = Project(user_id=user.id, name="Other", slug="other")
    db.add(other)
    db.commit()
    db.refresh(other)
    _publication(db, _piece(db, other, slug="theirs"), Platform.DEVTO)
    _publication(db, _piece(db, project, slug="ours"), Platform.DEVTO)

    with caplog.at_level(logging.WARNING, logger="app.routers.projects"):
        client.delete(f"{V1}/projects/{project.id}", headers=auth)

    line = _lines(caplog, "app.routers.projects", logging.WARNING)[0]
    assert "1 piece(s)" in line
    assert "1 publication(s)" in line


def test_somebody_elses_project_is_a_404_and_not_a_log_line(client, db, auth, caplog):
    """The 404 path must not report a deletion that did not happen."""
    from app.models.user import User
    from app.security import hash_password

    stranger = User(email="them@example.com", hashed_password=hash_password("hunter2hunter2"))
    db.add(stranger)
    db.commit()
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)

    with caplog.at_level(logging.WARNING, logger="app.routers.projects"):
        assert client.delete(f"{V1}/projects/{theirs.id}", headers=auth).status_code == 404

    assert _lines(caplog, "app.routers.projects", logging.WARNING) == []


# --------------------------------------------------------------------------- #
# Content                                                                      #
# --------------------------------------------------------------------------- #


def test_deleting_a_piece_says_how_much_of_it_stays_live(
    client, db, auth, user, project, caplog
):
    """The live count, not the total: those are the posts that outlive the row."""
    piece = _piece(db, project, slug="shipped")
    _publication(db, piece, Platform.DEVTO)
    _publication(db, piece, Platform.HASHNODE)
    _publication(db, piece, Platform.BLUESKY, status=PublicationStatus.FAILED)

    with caplog.at_level(logging.INFO, logger="app.routers.content"):
        assert client.delete(f"{V1}/content/{piece.id}", headers=auth).status_code == 204

    lines = _lines(caplog, "app.routers.content", logging.INFO)
    assert len(lines) == 1
    assert f"content {piece.id} (shipped) deleted" in lines[0]
    assert f"by user {user.id}" in lines[0]
    assert "2 publication(s) stay live" in lines[0]


def test_deleting_a_draft_nothing_was_published_from_is_ordinary_tidying(
    client, db, auth, project, caplog
):
    piece = _piece(db, project, slug="never-went-out")

    with caplog.at_level(logging.INFO, logger="app.routers.content"):
        client.delete(f"{V1}/content/{piece.id}", headers=auth)

    assert "0 publication(s) stay live" in _lines(caplog, "app.routers.content", logging.INFO)[0]


# --------------------------------------------------------------------------- #
# Connections                                                                  #
# --------------------------------------------------------------------------- #


def test_disconnecting_a_platform_is_recorded_as_the_cause_it_becomes(
    client, db, auth, user, connect, caplog
):
    """Queued rows fail as unconnected later, with nothing else linking the two."""
    connect(Platform.DEVTO)

    with caplog.at_level(logging.WARNING, logger="app.routers.settings"):
        resp = client.delete(f"{V1}/settings/connections/devto", headers=auth)
    assert resp.status_code == 204

    lines = _lines(caplog, "app.routers.settings", logging.WARNING)
    assert len(lines) == 1
    assert f"user {user.id} disconnected devto" in lines[0]
    assert db.scalar(
        PlatformConnection.__table__.select().where(
            PlatformConnection.user_id == user.id
        )
    ) is None


def test_disconnecting_something_that_was_not_connected_records_nothing(
    client, auth, caplog
):
    with caplog.at_level(logging.WARNING, logger="app.routers.settings"):
        assert client.delete(f"{V1}/settings/connections/devto", headers=auth).status_code == 404

    assert _lines(caplog, "app.routers.settings", logging.WARNING) == []


def test_a_reconnect_is_not_reported_as_a_disconnect(client, db, auth, user, caplog):
    """Overwriting credentials keeps the row; only a real delete is destructive."""
    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials="x",
        )
    )
    db.commit()

    with caplog.at_level(logging.WARNING, logger="app.routers.settings"):
        client.get(f"{V1}/settings/connections", headers=auth)

    assert _lines(caplog, "app.routers.settings", logging.WARNING) == []
