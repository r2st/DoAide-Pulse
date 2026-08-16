"""A connection says what language it publishes in; the publish path honours it.

The language lives on the *connection* rather than on the piece or the project,
because it is a property of the audience on the other end: the same release note
goes to an English Dev.to and a Japanese company blog, and neither the piece nor
the project can express that.

What the request swaps is exactly four fields — title, body, excerpt, meta
description — and nothing else. Not the slug, which is the filename a Git
destination writes and the ``utm_content`` on the share link; not the tags, which
are a taxonomy the destination indexes on; not the canonical, which must keep
pointing at the one original. There is one piece, and a language is an adaptation
of it in exactly the way a platform is.

The refusals matter more than the happy path here, because the failure this
feature can actually cause is publishing fluent prose that describes a version of
the article that no longer exists.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.models.translation import ContentTranslation, TranslationStatus
from app.services import crypto, publishing_service

V1 = "/api/v1"


@pytest.fixture
def piece(db, project) -> Content:
    """A piece with a canonical, a slug and tags — the fields that must not move."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        title="Herald 2.0 is out",
        slug="herald-2-0-is-out",
        body_markdown="# Herald 2.0\n\nThe release is available today.\n",
        excerpt="The release is available today.",
        meta_description="Herald 2.0, available today.",
        canonical_url="https://herald.example.com/blog/herald-2-0",
        tags=["release", "herald"],
        keywords=["release"],
        focus_keyword="release",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def french(db, piece) -> ContentTranslation:
    """A clean, current French translation of that piece."""
    row = ContentTranslation(
        content_id=piece.id,
        language="fr",
        status=TranslationStatus.READY,
        title="Herald 2.0 est disponible",
        body_markdown="# Herald 2.0\n\nLa version est disponible dès aujourd'hui.\n",
        excerpt="La version est disponible dès aujourd'hui.",
        meta_description="Herald 2.0, disponible aujourd'hui.",
        source_version=piece.version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _connection(db, user, *, platform=Platform.DEVTO, language="en") -> PlatformConnection:
    row = PlatformConnection(
        user_id=user.id,
        platform=platform,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=crypto.encrypt_credentials({"api_key": "k"}),
        language=language,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# What the request carries                                                      #
# --------------------------------------------------------------------------- #


def test_a_translation_replaces_the_four_fields_that_have_words_in_them(piece, french):
    """The swap, stated as what moves."""
    request = publishing_service.build_request(
        piece, platform=Platform.DEVTO, translation=french
    )

    assert request.title == french.title
    assert "disponible dès aujourd'hui" in request.body_markdown
    assert request.excerpt == french.excerpt
    assert request.meta_description == french.meta_description
    assert request.language == "fr"


def test_a_translation_does_not_move_the_slug_the_tags_or_the_canonical(piece, french):
    """And stated as what must not move, which is the half that causes damage.

    A per-language slug publishes the same piece to two paths and splits its
    analytics in half; a per-language canonical stops pointing at the original,
    which is worse than having no canonical at all.
    """
    request = publishing_service.build_request(
        piece, platform=Platform.DEVTO, translation=french
    )

    assert request.slug == piece.slug
    assert request.tags == piece.tags
    assert request.keywords == piece.keywords
    assert request.canonical_url == piece.canonical_url
    assert request.idempotency_key == f"herald-{piece.id}-devto"


def test_a_request_with_no_translation_is_exactly_what_it_always_was(piece):
    """Every existing caller passes nothing, and must be unaffected."""
    request = publishing_service.build_request(piece, platform=Platform.DEVTO)

    assert request.title == piece.title
    assert request.body_markdown.startswith("# Herald 2.0")
    assert request.language == "en"


# --------------------------------------------------------------------------- #
# Which translation the publish path picks                                      #
# --------------------------------------------------------------------------- #


def test_a_french_destination_gets_the_french_text(db, user, piece, french):
    """The happy path, resolved from the connection rather than from the piece."""
    _connection(db, user, language="fr")

    choice = publishing_service._translation_for(db, user.id, Platform.DEVTO, piece)

    assert choice.translation is french
    assert choice.language == "fr"


def test_an_english_destination_is_untouched_by_any_of_this(db, user, piece, french):
    """A connection that never sets a language publishes English, as it always did."""
    _connection(db, user, language="en")

    choice = publishing_service._translation_for(db, user.id, Platform.DEVTO, piece)

    assert choice.translation is None
    assert choice.language == "en"


def test_a_connection_defaults_to_english(db, user, piece, french):
    """The backfill puts the fleet into the state it is already in.

    Every existing connection publishes English today and continues to, which is
    why the column is NOT NULL with a server default rather than nullable.
    """
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=crypto.encrypt_credentials({"api_key": "k"}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    assert row.language == "en"
    assert publishing_service._translation_for(
        db, user.id, Platform.DEVTO, piece
    ).translation is None


def test_a_french_destination_falls_back_when_the_piece_was_edited_after_translating(
    db, user, piece, french
):
    """The refusal the whole ``source_version`` design exists for.

    A stale translation is fluent: it would publish, and read, and be wrong.
    Publishing the English instead is a visibly worse outcome the user can see
    and fix, which makes it the better one.
    """
    _connection(db, user, language="fr")
    piece.body_markdown = "# Herald 2.0\n\nThe release is available today, with fixes.\n"
    db.commit()
    db.refresh(piece)

    choice = publishing_service._translation_for(db, user.id, Platform.DEVTO, piece)

    assert choice.translation is None
    assert choice.language == "en"
    assert "out of date" in choice.reason


def test_two_destinations_can_want_two_different_languages(db, user, piece, french):
    """The case that motivates putting the language on the connection at all."""
    _connection(db, user, platform=Platform.DEVTO, language="en")
    _connection(db, user, platform=Platform.HASHNODE, language="fr")

    devto = publishing_service._translation_for(db, user.id, Platform.DEVTO, piece)
    hashnode = publishing_service._translation_for(db, user.id, Platform.HASHNODE, piece)

    assert devto.translation is None
    assert hashnode.translation is french


def test_the_choice_always_carries_a_reason(db, user, piece):
    """A silent fallback is indistinguishable from a feature that does not work.

    Every branch reports one, including the ones that publish English, because
    the log line is where a surprised user goes looking.
    """
    _connection(db, user, language="ja")

    choice = publishing_service._translation_for(db, user.id, Platform.DEVTO, piece)

    assert choice.reason
    assert choice.as_dict() == {
        "language": "en",
        "translated": False,
        "reason": choice.reason,
    }


def test_a_destination_with_no_connection_publishes_the_original(db, user, piece, french):
    """The branch for callers that build a request without one — previews, checks."""
    choice = publishing_service._translation_for(db, user.id, Platform.MEDIUM, piece)

    assert choice.translation is None
    assert choice.language == "en"


# --------------------------------------------------------------------------- #
# End to end                                                                    #
# --------------------------------------------------------------------------- #


def test_the_text_that_reaches_the_adapter_is_the_translated_one(
    db, user, piece, french, monkeypatch
):
    """The whole path, from the connection's language to the adapter's argument.

    The unit tests above each pin one link. This is the one that fails if the
    resolution is wired to the wrong place — computed correctly and then not
    passed, which is the shape that unit tests either side of it both miss.
    """
    from app.models.publication import Publication, PublicationStatus
    from app.services.publishers.base import PublishResult
    from app.services.publishers.devto import DevToAdapter

    _connection(db, user, language="fr")
    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    seen = {}

    def _capture(_self, request, _credentials):
        seen["title"] = request.title
        seen["language"] = request.language
        return PublishResult(external_id="1", external_url="https://dev.to/x/1")

    monkeypatch.setattr(DevToAdapter, "publish", _capture)

    publishing_service.execute(db, publication)

    assert seen["title"] == "Herald 2.0 est disponible"
    assert seen["language"] == "fr"


def test_a_stale_translation_does_not_reach_the_adapter(
    db, user, piece, french, monkeypatch
):
    """The same path, proving the refusal is on it rather than beside it."""
    from app.models.publication import Publication, PublicationStatus
    from app.services.publishers.base import PublishResult
    from app.services.publishers.devto import DevToAdapter

    _connection(db, user, language="fr")
    piece.title = "Herald 2.0.1 is out"
    db.commit()

    publication = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    seen = {}

    def _capture(_self, request, _credentials):
        seen["title"] = request.title
        seen["language"] = request.language
        return PublishResult(external_id="1", external_url="https://dev.to/x/1")

    monkeypatch.setattr(DevToAdapter, "publish", _capture)

    publishing_service.execute(db, publication)

    assert seen["title"] == "Herald 2.0.1 is out"
    assert seen["language"] == "en"
