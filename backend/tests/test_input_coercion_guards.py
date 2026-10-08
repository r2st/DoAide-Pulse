"""The small guards that turn hostile or sloppy input into "nothing" safely.

Everything here is a one-or-two-line arm that decides whether a bad value is
refused, dropped, or quietly carried forward. They are individually tiny and
collectively the reason a malformed token, a blank image URL or a model's
half-written JSON does not become a 500.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import security
from app.deps import get_current_user
from app.models.content import Content, ContentStatus, ContentType
from app.models.preview_link import PreviewLink
from app.schemas.content import ContentUpdate
from app.schemas.webhook import WebhookUpdate
from app.services import ai, utm

# --------------------------------------------------------------------------- #
# Tokens                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "iat", [None, "not a number", 10**30, float("nan"), -(10**30)]
)
def test_an_unusable_issued_at_claim_reads_as_no_issued_at(iat):
    """``tokens_valid_from`` compares against this; a crash would 500 the request."""
    assert security.issued_at({"iat": iat} if iat is not None else {}) is None


def test_a_subject_that_is_not_an_integer_is_refused_as_a_credential(db):
    token = security.create_access_token("not-an-int")

    with pytest.raises(HTTPException) as caught:
        get_current_user(token=token, db=db)

    assert caught.value.status_code == 401


# --------------------------------------------------------------------------- #
# URLs on content                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["cover_image_url", "canonical_url"])
def test_a_url_field_left_blank_is_cleared_rather_than_stored_as_empty(field):
    """The platforms treat "" as a URL. ``None`` is the only honest absence."""
    assert getattr(ContentUpdate(**{field: "   "}), field) is None
    assert getattr(ContentUpdate(**{field: None}), field) is None


@pytest.mark.parametrize("field", ["cover_image_url", "canonical_url"])
def test_a_relative_url_is_refused_because_it_resolves_somewhere_else(field):
    with pytest.raises(ValidationError):
        ContentUpdate(**{field: "/static/cover.png"})


# --------------------------------------------------------------------------- #
# Webhook patches                                                             #
# --------------------------------------------------------------------------- #


def test_omitting_events_from_a_webhook_patch_leaves_them_alone():
    assert WebhookUpdate().events is None
    assert WebhookUpdate(description="x").events is None


def test_supplying_events_on_a_patch_still_runs_them_through_validation():
    assert WebhookUpdate(events=["content.published"]).events == ["content.published"]

    with pytest.raises(ValidationError):
        WebhookUpdate(events=[])


# --------------------------------------------------------------------------- #
# Model output                                                                #
# --------------------------------------------------------------------------- #


def test_an_empty_completion_yields_no_object():
    assert ai.extract_json_object("") is None
    assert ai.extract_json_object(None) is None


def test_a_scratchpad_full_of_broken_json_yields_no_object():
    """Balanced braces are not the same as parseable JSON."""
    assert ai.extract_json_object("thinking… {not: valid} and {also, not}") is None


def test_the_largest_complete_object_in_a_scratchpad_wins():
    raw = 'plan: {"title": "x"} then the real one: {"title": "A Real Title", "body": "..."}'

    assert ai.extract_json_object(raw)["title"] == "A Real Title"


def test_a_missing_list_field_coerces_to_an_empty_list():
    assert ai.as_str_list(None) == []


# --------------------------------------------------------------------------- #
# UTM                                                                         #
# --------------------------------------------------------------------------- #


def test_a_url_that_already_carries_every_utm_key_is_returned_untouched():
    url = (
        "https://example.com/post?utm_source=devto&utm_medium=social"
        "&utm_campaign=pulse"
    )

    assert utm.tag(url, source="devto", medium="social", campaign="pulse") == url


def test_rewriting_links_in_an_empty_body_or_with_no_host_is_a_no_op():
    """Rewriting somebody's prose is not a thing to do approximately."""
    assert utm.tag_markdown_links("", host="example.com", utm_source="devto") == ""

    body = "[a](https://example.com/x)"
    assert utm.tag_markdown_links(body, host="", utm_source="devto") == body


# --------------------------------------------------------------------------- #
# Preview links                                                               #
# --------------------------------------------------------------------------- #


def _piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        status=ContentStatus.REVIEW,
        title="A",
        slug="a",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_a_link_is_live_until_it_is_revoked_or_expires(db, project):
    content = _piece(db, project)
    ahead = datetime.now(UTC) + timedelta(days=3)

    live = PreviewLink(content_id=content.id, token_hash="a" * 64, expires_at=ahead)
    revoked = PreviewLink(
        content_id=content.id,
        token_hash="b" * 64,
        expires_at=ahead,
        revoked_at=datetime.now(UTC),
    )
    expired = PreviewLink(
        content_id=content.id,
        token_hash="c" * 64,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add_all([live, revoked, expired])
    db.commit()

    assert live.is_live is True
    assert revoked.is_live is False
    assert expired.is_live is False
