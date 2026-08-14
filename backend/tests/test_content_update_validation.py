"""What ``PATCH /content/{id}`` is allowed to write.

Two fields on ``ContentUpdate`` took anything the enum or the column allowed:

* ``status`` accepted ``published`` and ``failed``, which are *derived* from a
  piece's publications. Setting either by hand makes the content row disagree
  with what is live — a piece counted in the analytics as published with nothing
  behind it, or one marked failed while a publication is still in flight — and
  the next successful publish silently overwrites the lie.
* ``canonical_url`` accepted any string under 700 characters. It is sent
  verbatim as ``rel=canonical`` by every adapter that supports one, so a
  relative path resolves against the syndicating platform's host and points the
  crawler at dev.to.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.models.publication import Publication


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="word " * 200,
        excerpt="x",
        meta_description="x",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _patch(client, auth, content, body):
    return client.patch(f"/api/v1/content/{content.id}", headers=auth, json=body)


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["published", "failed"])
def test_a_derived_status_cannot_be_set_by_hand(client, auth, db, content, value):
    resp = _patch(client, auth, content, {"status": value})

    assert resp.status_code == 422
    db.refresh(content)
    assert content.status == ContentStatus.DRAFT


@pytest.mark.parametrize("value", ["draft", "review", "approved", "archived"])
def test_the_editorial_statuses_still_work(client, auth, db, content, value):
    resp = _patch(client, auth, content, {"status": value})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == value


def test_a_published_piece_cannot_be_moved_back_to_draft(client, auth, db, content):
    """It is still live everywhere it went — saying otherwise helps nobody."""
    content.status = ContentStatus.PUBLISHED
    db.commit()

    resp = _patch(client, auth, content, {"status": "draft"})

    assert resp.status_code == 409
    db.refresh(content)
    assert content.status == ContentStatus.PUBLISHED


def test_a_published_piece_can_still_be_archived(client, auth, db, content):
    """Archiving means "stop showing me this", not "this never went out"."""
    content.status = ContentStatus.PUBLISHED
    db.commit()

    resp = _patch(client, auth, content, {"status": "archived"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"


def test_approving_through_the_patch_releases_it_too(
    client, auth, db, project, monkeypatch
):
    """A scripted caller should not need to know about a second endpoint."""
    from app.services import content_pipeline

    monkeypatch.setattr(content_pipeline, "publish_now", lambda pid: None)
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    db.commit()
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Ship it",
        slug="ship-it",
        body_markdown="word " * 200,
        excerpt="x",
        meta_description="x",
        status=ContentStatus.REVIEW,
    )
    db.add(row)
    db.commit()

    resp = client.patch(
        f"/api/v1/content/{row.id}", headers=auth, json={"status": "approved"}
    )

    assert resp.status_code == 200, resp.text
    assert db.query(Publication).count() == 1


# --------------------------------------------------------------------------- #
# canonical_url                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "/blog/herald-1-0",
        "herald.example.com/blog",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_a_canonical_that_is_not_an_absolute_http_url_is_refused(
    client, auth, content, value
):
    resp = _patch(client, auth, content, {"canonical_url": value})
    assert resp.status_code == 422


def test_an_absolute_canonical_is_accepted(client, auth, content):
    resp = _patch(
        client, auth, content, {"canonical_url": "https://herald.example.com/blog/x"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["canonical_url"] == "https://herald.example.com/blog/x"


def test_clearing_the_canonical_still_works(client, auth, db, content):
    content.canonical_url = "https://herald.example.com/blog/x"
    db.commit()

    resp = _patch(client, auth, content, {"canonical_url": ""})

    assert resp.status_code == 200, resp.text
    assert resp.json()["canonical_url"] is None


def test_creating_with_a_relative_canonical_is_refused(client, auth, project):
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Hand written",
            "body_markdown": "x",
            "canonical_url": "/blog/hand-written",
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Lengths the column has to be able to hold                                    #
# --------------------------------------------------------------------------- #
#
# Both fields below accepted more than their column: ``meta_description`` took
# 500 characters into a ``String(320)``, ``canonical_url`` 700 into a
# ``String(500)``. On SQLite that is invisible — VARCHAR lengths are not
# enforced, so the value went in and every assertion about it passed. On
# PostgreSQL it is a ``StringDataRightTruncation``, which is a ``DataError`` and
# not an ``IntegrityError``, so the retry in ``_commit_content`` does not catch
# it and neither does anything else: the caller gets a 500 for a body the API
# had already validated.
#
# So these tests assert the *status code*, which is the part SQLite cannot lie
# about. ``tests/test_schema_caps_fit_their_columns.py`` pins the numbers
# themselves against the mappers.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("meta_description", "m" * 321),
        ("canonical_url", "https://herald.example.com/" + "p" * 480),
    ],
)
def test_a_value_too_long_for_its_column_is_refused_on_patch(
    client, auth, db, content, field, value
):
    assert len(value) > 320 if field == "meta_description" else len(value) > 500

    resp = _patch(client, auth, content, {field: value})

    assert resp.status_code == 422, resp.text
    db.refresh(content)
    assert getattr(content, field) != value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("meta_description", "m" * 321),
        ("canonical_url", "https://herald.example.com/" + "p" * 480),
    ],
)
def test_a_value_too_long_for_its_column_is_refused_on_create(
    client, auth, project, field, value
):
    resp = client.post(
        "/api/v1/content",
        headers=auth,
        json={
            "project_id": project.id,
            "title": "Hand written",
            "body_markdown": "x",
            field: value,
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("meta_description", "m" * 320),
        ("canonical_url", "https://a.example.com/" + "p" * 478),
    ],
)
def test_a_value_that_exactly_fills_its_column_is_still_accepted(
    client, auth, content, field, value
):
    """The other edge: narrowing a cap must not cost the last usable character."""
    resp = _patch(client, auth, content, {field: value})

    assert resp.status_code == 200, resp.text
    assert resp.json()[field] == value
