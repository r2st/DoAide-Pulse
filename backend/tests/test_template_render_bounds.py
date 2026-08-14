"""A template bounds its inputs. Nothing bounded its output.

Every input to a render was already capped: ``title_template`` at 300
characters, ``body_template`` at 50,000, twenty-five variables, and each
substituted value at ``VALUE_LIMIT`` (5,000). None of that bounds the result,
because a placeholder may repeat — and the amplification is not subtle. A
50,000-character body template holds about ten thousand copies of ``{{v}}``, so
a 48 KB request rendered a 40 MB body: straight past the 1 MB body-size
middleware, into a ``Text`` column with no width to stop it, and from there into
every adapter request, SEO audit and editor load for that piece, forever. The
endpoint that does it costs one rate-limit token.

The title was the narrower version with the worse landing. ``Content.title`` is
``String(300)`` and ``slug`` is ``String(320)``, so a template whose whole title
is one ``{{v}}`` produced a 5,000-character title and a 5,000-character slug —
on PostgreSQL a ``DataError`` for the title and, because ``slug`` carries the
``uq_content_project_slug`` unique index, an "index row size exceeds maximum"
for the slug. On SQLite both are stored without complaint, which is why the
suite was green.

The split follows the one the module already draws between its two endpoints: a
preview renders anyway and says what it clipped, because seeing the first 200 KB
of what a template produces is how an author discovers it produces too much. The
endpoint that writes a row refuses.
"""
from __future__ import annotations

import pytest

from app.models.content import (
    BODY_MARKDOWN_MAX_LENGTH,
    SLUG_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    Content,
)
from app.services import templates as template_service

API = "/api/v1/templates"


def _make(client, auth, **overrides):
    body = {
        "name": overrides.pop("name", "Release"),
        "mode": "literal",
        "content_type": "announcement",
        "title_template": "{{v}}",
        "body_template": "Shipped {{v}}.",
        "variables": [{"name": "v"}],
        **overrides,
    }
    resp = client.post(API, headers=auth, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _use(client, auth, template_id, project, values):
    return client.post(
        f"{API}/{template_id}/use",
        headers=auth,
        json={"project_id": project.id, "values": values},
    )


def _preview(client, auth, template_id, project, values):
    return client.post(
        f"{API}/{template_id}/preview",
        headers=auth,
        json={"project_id": project.id, "values": values},
    )


# --------------------------------------------------------------------------- #
# The limits themselves                                                        #
# --------------------------------------------------------------------------- #


def test_the_render_limits_are_the_ones_the_content_schema_enforces():
    """A template must not be a way around the caps ``POST /content`` applies.

    Pinned by identity rather than by value: if someone widens what a
    hand-written piece may hold, the template path should follow, and if
    someone narrows it the template path must not be left behind.
    """
    assert template_service.TITLE_LIMIT == TITLE_MAX_LENGTH
    assert template_service.BODY_LIMIT == BODY_MARKDOWN_MAX_LENGTH


# --------------------------------------------------------------------------- #
# The title                                                                    #
# --------------------------------------------------------------------------- #


def test_a_title_rendered_past_the_column_is_refused(client, auth, project):
    template_id = _make(client, auth)

    resp = _use(client, auth, template_id, project, {"v": "x" * 5000})

    assert resp.status_code == 422, resp.text
    assert "title" in resp.json()["detail"]


def test_nothing_is_written_when_the_render_is_refused(client, auth, project, db):
    template_id = _make(client, auth)

    _use(client, auth, template_id, project, {"v": "x" * 5000})

    assert db.query(Content).count() == 0


def test_a_title_that_fits_still_writes_the_piece(client, auth, project):
    """The boundary, so the guard cannot drift into refusing ordinary templates."""
    template_id = _make(client, auth)

    resp = _use(client, auth, template_id, project, {"v": "x" * TITLE_MAX_LENGTH})

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["title"]) == TITLE_MAX_LENGTH


def test_the_slug_stays_inside_its_column_for_the_longest_legal_title(
    client, auth, project, db
):
    """The slug is derived, so it is bounded by the title *plus* a suffix.

    Worth its own test because the slug is the unique-index key: it is the field
    whose overflow fails differently, and the one no request names directly.
    """
    template_id = _make(client, auth)

    resp = _use(client, auth, template_id, project, {"v": "x" * TITLE_MAX_LENGTH})

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["slug"]) <= SLUG_MAX_LENGTH


def test_a_second_piece_with_the_longest_title_still_fits_with_its_suffix(
    client, auth, project
):
    """The collision suffix is appended to an already-maximal slug.

    ``unique_content_slug`` reserves room for it rather than assuming the base
    is short, which is the case that would otherwise push the slug over its
    column only on the *second* use of a template.
    """
    template_id = _make(client, auth)
    values = {"v": "x" * TITLE_MAX_LENGTH}

    first = _use(client, auth, template_id, project, values)
    second = _use(client, auth, template_id, project, values)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["slug"] != second.json()["slug"]
    assert len(second.json()["slug"]) <= SLUG_MAX_LENGTH


# --------------------------------------------------------------------------- #
# The body                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def amplifier(client, auth):
    """A template that turns one 5 KB value into tens of megabytes.

    8,000 placeholders is a ~48 KB ``body_template``, comfortably inside both
    its own 50,000-character cap and the 1 MB request ceiling.
    """
    return _make(
        client,
        auth,
        name="Amplifier",
        title_template="Release",
        body_template="{{v}}\n" * 8000,
    )


def test_a_body_rendered_past_the_limit_is_refused(client, auth, project, amplifier):
    resp = _use(client, auth, amplifier, project, {"v": "x" * 5000})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "body" in detail
    # The message names the number rather than leaving the author to guess
    # which of 8,000 placeholders to blame.
    assert f"{BODY_MARKDOWN_MAX_LENGTH:,}" in detail


def test_the_amplification_is_real_and_not_a_theory(
    client, auth, project, amplifier, db, monkeypatch
):
    """What the render produces with the ceiling lifted, measured rather than argued.

    This is the number that makes the guard worth having. Lifting ``BODY_LIMIT``
    for one call reproduces the old behaviour exactly, so if a later change to
    ``VALUE_LIMIT`` or the template caps removes the amplification on its own,
    this fails and says the guard has become decorative — rather than leaving
    behind a limit nobody can justify.
    """
    from app.models.template import ContentTemplate

    monkeypatch.setattr(template_service, "BODY_LIMIT", 10**9)
    template = db.get(ContentTemplate, amplifier)

    rendered = template_service.render(template, {"v": "x" * 5000})

    request_bytes = len(template.body_template)
    assert request_bytes < 1024 * 1024  # sails through the body-size middleware
    assert len(rendered.body) > 39_000_000  # ~40 MB into a Text column
    assert rendered.over_limit == []  # because the ceiling is what stops it


def test_a_body_that_fits_is_still_written(client, auth, project):
    template_id = _make(
        client, auth, name="Modest", title_template="Release", body_template="{{v}}"
    )

    resp = _use(client, auth, template_id, project, {"v": "x" * 5000})

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["body_markdown"]) == 5000


# --------------------------------------------------------------------------- #
# The preview renders anyway                                                   #
# --------------------------------------------------------------------------- #


def test_the_preview_clips_rather_than_refusing(client, auth, project, amplifier):
    """Seeing the first 200 KB is how the author finds out there is too much."""
    resp = _preview(client, auth, amplifier, project, {"v": "x" * 5000})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["over_limit"] == ["body"]
    assert len(body["body"]) == BODY_MARKDOWN_MAX_LENGTH


def test_the_preview_reports_a_clipped_title(client, auth, project):
    template_id = _make(client, auth)

    resp = _preview(client, auth, template_id, project, {"v": "x" * 5000})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["over_limit"] == ["title"]
    assert len(body["title"]) == TITLE_MAX_LENGTH


def test_an_ordinary_preview_reports_nothing_clipped(client, auth, project):
    template_id = _make(client, auth)

    resp = _preview(client, auth, template_id, project, {"v": "1.2.0"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["over_limit"] == []


def test_both_fields_are_reported_when_both_overflow(client, auth, project):
    template_id = _make(
        client,
        auth,
        name="Both",
        title_template="{{v}}",
        body_template="{{v}}\n" * 8000,
    )

    resp = _preview(client, auth, template_id, project, {"v": "x" * 5000})

    assert resp.status_code == 200, resp.text
    assert resp.json()["over_limit"] == ["title", "body"]


def test_using_that_template_names_both_in_one_refusal(client, auth, project):
    template_id = _make(
        client,
        auth,
        name="Both",
        title_template="{{v}}",
        body_template="{{v}}\n" * 8000,
    )

    resp = _use(client, auth, template_id, project, {"v": "x" * 5000})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "title" in detail and "body" in detail


# --------------------------------------------------------------------------- #
# Measured after the tidying passes, not before                                #
# --------------------------------------------------------------------------- #


def test_a_title_that_only_fits_once_whitespace_is_collapsed_is_accepted(
    client, auth, project
):
    """``render`` collapses runs of whitespace in the title before measuring it.

    Order matters, and the title is where it is visible: a 5,000-character value
    that is almost entirely spaces is three characters of actual title. Checking
    the length first would refuse a piece whose title is "a b" on the grounds
    that the value behind it was long, which is not what the limit is about.
    """
    template_id = _make(client, auth)
    padded = "a" + " " * 4998 + "b"
    assert len(padded) > TITLE_MAX_LENGTH

    resp = _use(client, auth, template_id, project, {"v": padded})

    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "a b"


def test_a_body_that_only_fits_once_empty_lines_are_dropped_is_accepted(
    client, auth, project
):
    """The same ordering on the body, via ``drop_empty_lines``.

    ``- {{v}}`` is scaffolding with no words of the author's own, so an empty
    value takes the whole line with it — and the result is measured after that
    has happened.
    """
    template_id = _make(
        client,
        auth,
        name="Sparse",
        title_template="Release",
        body_template="Notes\n\n" + "- {{v}}\n" * 6000,
        variables=[{"name": "v"}],
    )

    resp = _use(client, auth, template_id, project, {"v": ""})

    assert resp.status_code == 201, resp.text
    assert resp.json()["body_markdown"] == "Notes"
