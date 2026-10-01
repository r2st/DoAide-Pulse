"""The model does not get to decide how long a row is.

``BODY_MARKDOWN_MAX_LENGTH`` describes itself as "the ceiling ``ContentCreate``
has always applied, named here so the paths that build a body *without* going
through that schema apply the same one", and ``TAG_MAX_LENGTH`` makes the same
claim about a tag that "met nothing at all between the request body and the
adapter that posts it".

There are three places a ``Content`` row is built. Two honoured both caps —
``routers.content.create_content`` behind ``ContentCreate``, and
``routers.templates`` behind ``templates.BODY_LIMIT``. The third,
:func:`app.services.content_generator.content_from_generated`, is the one every
generation goes through: the Generate button, ``POST /ideas/{id}/write``, the
autopilot scan, and every trigger that fires a write. It applied neither. The
body and the tag list went into the row exactly as the model emitted them.

What bounds a model's output, then, is ``llm_router.MAX_RESPONSE_BYTES`` — four
megabytes, twenty times the cap the API enforces on the same column. Nothing
raises: ``body_markdown`` is ``Text`` and ``tags`` is JSON, so both columns take
whatever they are given. The cost is paid afterwards and forever, by every
listing that selects the row, every adapter that renders it, every SEO audit
that reads it and every publish that flattens it to plain text once per
platform — inside a Celery task with a soft time limit.

A generation reaching either cap is a malfunction rather than an unusually
thorough article: the longest shape Pulse asks for is a 1200-word tutorial, and
the token budget derived from that cannot produce thirty thousand words. So the
caps here are not an opinion about length. They are where a runaway completion
stops being stored whole — the same judgement ``Idea.__post_init__`` and
``github_client._text`` already make about the columns they feed.
"""
from __future__ import annotations

import pytest

from app.models.content import (
    BODY_MARKDOWN_MAX_LENGTH,
    TAG_MAX_LENGTH,
    Content,
    ContentStatus,
    ContentType,
    clamp_body,
    clamp_tags,
)
from app.schemas.content import ContentCreate
from app.schemas.limits import Tag
from app.services import content_generator
from app.services.content_generator import GeneratedContent

# --------------------------------------------------------------------------- #
# clamp_body                                                                   #
# --------------------------------------------------------------------------- #


def test_a_body_inside_the_cap_is_returned_untouched():
    body = "## Retries\n\nA paragraph.\n\nAnother paragraph.\n"
    assert clamp_body(body) is body


def test_a_body_at_exactly_the_cap_is_returned_untouched():
    body = "x" * BODY_MARKDOWN_MAX_LENGTH
    assert clamp_body(body) == body


def test_an_over_long_body_never_comes_back_over_the_cap():
    assert len(clamp_body("word " * 200_000)) <= BODY_MARKDOWN_MAX_LENGTH


def test_the_cut_lands_on_a_paragraph_break():
    """A hard slice can land inside a fence or a link and change what renders."""
    paragraph = "Some prose about the retry path.\n\n"
    body = paragraph * 20_000
    clipped = clamp_body(body, limit=1000)

    assert len(clipped) <= 1000
    # Whole paragraphs, and no dangling blank line where the cut landed.
    assert clipped.endswith("retry path.")
    assert clipped + "\n\n" == paragraph * (clipped.count("\n\n") + 1)


def test_a_body_with_no_break_in_reach_is_cut_hard():
    """One paragraph at character 300 must not cut a 1000-character budget to 300.

    Preferring a break unconditionally is how a body with a single early blank
    line loses two thirds of itself to a rule meant to protect the last few
    characters.
    """
    body = "intro\n\n" + "x" * 5_000
    clipped = clamp_body(body, limit=1000)
    assert len(clipped) == 1000
    assert clipped.startswith("intro")


def test_the_default_limit_is_the_one_the_api_enforces():
    """Two ceilings on one column would be a bug wearing a constant's name."""
    assert (
        ContentCreate.model_fields["body_markdown"].metadata[0].max_length
        == BODY_MARKDOWN_MAX_LENGTH
    )
    assert len(clamp_body("x" * (BODY_MARKDOWN_MAX_LENGTH + 1))) <= (
        BODY_MARKDOWN_MAX_LENGTH
    )


# --------------------------------------------------------------------------- #
# clamp_tags                                                                   #
# --------------------------------------------------------------------------- #


def test_ordinary_tags_pass_through():
    assert clamp_tags(["python", "fastapi"]) == ["python", "fastapi"]


def test_an_over_long_tag_is_cut_to_the_cap():
    clamped = clamp_tags(["a" * 5_000, "python"])
    assert clamped == ["a" * TAG_MAX_LENGTH, "python"]


def test_a_tag_that_is_all_whitespace_is_dropped():
    """Cutting can leave nothing behind, and an empty tag is not a tag."""
    assert clamp_tags(["   ", "", "python"]) == ["python"]


def test_the_tag_cap_is_the_one_the_api_enforces():
    """The schema side of this pair is pinned in ``test_list_items_are_bounded``.

    What is asserted here is that the generated path lands on the same number,
    rather than on one of its own that happens to be close.
    """
    assert clamp_tags(["a" * (TAG_MAX_LENGTH + 1)]) == [Tag.__metadata__[0].max_length * "a"]


# --------------------------------------------------------------------------- #
# The funnel applies both                                                      #
# --------------------------------------------------------------------------- #


def _generated(**overrides) -> GeneratedContent:
    fields = {
        "title": "Retries in Pulse",
        "body_markdown": "## Retries\n\nA paragraph about the retry path.",
        "excerpt": "How Pulse retries.",
        "meta_description": "How Pulse retries a failed publish.",
        "keywords": ["retries"],
        "tags": ["python"],
    }
    fields.update(overrides)
    return GeneratedContent(**fields)


def test_a_runaway_body_is_bounded_before_it_reaches_the_row(db, project):
    row = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        generated=_generated(body_markdown="A paragraph of prose.\n\n" * 40_000),
        status=ContentStatus.DRAFT,
        source={"kind": "test"},
    )
    assert len(row.body_markdown) <= BODY_MARKDOWN_MAX_LENGTH


def test_a_runaway_tag_is_bounded_before_it_reaches_the_row(db, project):
    row = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        generated=_generated(tags=["python", "b" * 4_000]),
        status=ContentStatus.DRAFT,
        source={"kind": "test"},
    )
    assert [len(tag) for tag in row.tags] == [6, TAG_MAX_LENGTH]


def test_the_bounded_row_still_stores_and_reads_back(db, project):
    """The point of the cap is a row the rest of the system can carry."""
    row = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        generated=_generated(body_markdown="A paragraph of prose.\n\n" * 40_000),
        status=ContentStatus.DRAFT,
        source={"kind": "test"},
    )
    db.add(row)
    db.commit()
    db.expire_all()

    stored = db.get(Content, row.id)
    assert len(stored.body_markdown) <= BODY_MARKDOWN_MAX_LENGTH
    # The denormalised count is derived from what was *stored*, not from what
    # the model sent — otherwise the row disagrees with itself.
    assert stored.word_count == len(stored.body_markdown.split())


def test_an_ordinary_generation_is_not_touched(db, project):
    generated = _generated()
    row = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        generated=generated,
        status=ContentStatus.DRAFT,
        source={"kind": "test"},
    )
    assert row.body_markdown == generated.body_markdown
    assert row.tags == generated.tags


def test_the_truncation_is_logged(db, project, caplog):
    """Silent truncation reads as "the model wrote a short article"."""
    with caplog.at_level("WARNING"):
        content_generator.content_from_generated(
            db,
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            generated=_generated(body_markdown="A paragraph of prose.\n\n" * 40_000),
            status=ContentStatus.DRAFT,
            source={"kind": "test"},
        )
    assert any("generated body" in record.message for record in caplog.records)


@pytest.mark.parametrize("limit", [1, 2, 11, 999])
def test_no_limit_produces_a_body_over_its_own_budget(limit):
    """Including the small ones, where ``limit // 10`` is zero."""
    assert len(clamp_body("para\n\npara\n\npara", limit=limit)) <= limit
