"""A piece in another language, and how much Pulse trusts it.

A translation is stored beside the piece rather than as a piece of its own, and
that is the whole design decision. The alternative — copy the row, set a
``language`` column, let it drift — gives you two rows that were the same
article on Tuesday and are two different articles by Friday, each with its own
publications, its own metrics and its own answer to "how did the launch post
do". Pulse already made this choice once, in the first line of
:mod:`app.models.content`: content is stored once and adapted per platform at
publish time, "so that a post that goes to Dev.to and LinkedIn is genuinely the
same piece rather than two divergent copies". A language is another axis of the
same adaptation.

So the English row stays the article. A translation carries only the fields that
have words in them, and inherits everything else — status, schedule, project,
tags-as-taxonomy, publications, analytics — from the piece it hangs off. Nothing
here can be published on its own, unpublished on its own, or scheduled
separately; there is one piece, and it goes out in as many languages as the
destinations want.

**Staleness is the hard part, and it is why :attr:`source_version` exists.** A
translation is only correct with respect to the text it was made from. Edit the
English body and the French one is now a translation of a paragraph that no
longer exists — still fluent, still publishable-looking, and wrong. Every
system that gets this wrong gets it wrong the same way: it stores the
translation and forgets what it translated. Pinning the source's
``Content.version`` at translation time makes the check a comparison of two
integers, on a counter that already increments on every write for the conflict
machinery, with no hashing and no guessing. See
:attr:`ContentTranslation.is_stale`.

**Quality is recorded, not assumed.** :attr:`quality_issues` holds what
:mod:`app.services.translation` found wrong — a body that came back the same
length as an English one it should be twenty percent longer than, a code fence
that lost its contents, a link that changed target, a script the target language
is not written in. The row is stored either way, because a translation with two
warnings on it is worth a human's five minutes and a deleted one is worth
nothing; but a translation with issues cannot be published automatically, on the
same principle as every other gate in
:mod:`app.services.content_pipeline`.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.content import META_DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.content import Content

#: Width of the language column. BCP-47 base tags are two or three characters;
#: the column is wider than it needs to be because a stored value is normalised
#: through :func:`app.services.languages.normalize` and a column that cannot
#: hold what a caller sent turns a 422 into a 500 on PostgreSQL. See the note on
#: column widths in :mod:`app.models.content`.
LANGUAGE_MAX_LENGTH = 16


class TranslationStatus(StrEnum):
    """Where a translation is in its short life.

    Four states rather than a boolean because "there is no French yet" and
    "French was attempted and the model refused" are different answers to the
    same question, and a UI that shows them identically sends the user to press
    the same button again.
    """

    #: Requested; a worker has not produced it yet.
    PENDING = "pending"
    #: Translated and validated clean. The only state that may publish
    #: unattended.
    READY = "ready"
    #: Translated, but :attr:`ContentTranslation.quality_issues` is non-empty.
    #: Publishable by a human who has read it; never automatically.
    NEEDS_REVIEW = "needs_review"
    #: The attempt failed — no provider answered, or the answer was unusable.
    #: :attr:`ContentTranslation.error` says which.
    FAILED = "failed"

    @property
    def label(self) -> str:
        """The state in the words the language list shows next to the flag."""
        return {
            "pending": "Waiting",
            "ready": "Ready",
            "needs_review": "Needs review",
            "failed": "Failed",
        }[self.value]


class ContentTranslation(Base, TimestampMixin):
    """One piece of content, rendered in one other language."""

    __tablename__ = "content_translations"
    __table_args__ = (
        # One translation per language per piece. The uniqueness is the feature:
        # re-translating replaces the row rather than accumulating candidates,
        # so "the French version" always names exactly one thing.
        UniqueConstraint("content_id", "language", name="uq_translation_per_language"),
        Index("ix_translation_content_language", "content_id", "language"),
        CheckConstraint("source_version >= 0", name="ck_translation_source_version_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: A base language tag from :data:`app.services.languages.TARGET_CODES`,
    #: already normalised — ``fr``, never ``fr-CA``.
    language: Mapped[str] = mapped_column(String(LANGUAGE_MAX_LENGTH), nullable=False)

    status: Mapped[TranslationStatus] = mapped_column(
        SAEnum(TranslationStatus, native_enum=False, length=20),
        default=TranslationStatus.PENDING,
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), default="", nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meta_description: Mapped[str] = mapped_column(
        String(META_DESCRIPTION_MAX_LENGTH), default="", nullable=False
    )

    #: The ``Content.version`` this text was translated from. ``0`` for a row
    #: that has not been translated yet, which is younger than any real version
    #: and therefore reads as stale — correct, because a pending row has no text
    #: and nothing should publish it.
    source_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: What validation found, as ``{"code": ..., "message": ...}`` objects. See
    #: :mod:`app.services.translation`. Empty is the good case and the default.
    quality_issues: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    #: Why the attempt failed, when it did. User-facing, bounded.
    error: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    generated_by_provider: Mapped[str | None] = mapped_column(String(40))
    generated_by_model: Mapped[str | None] = mapped_column(String(120))
    translated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    content: Mapped[Content] = relationship(back_populates="translations")

    def is_stale(self, content: Content | None = None) -> bool:
        """Whether the piece has been written since this translation was made.

        Takes the piece as an argument rather than reaching through
        :attr:`content`, because every caller that asks this is already holding
        it — the list endpoint has it, the publish path has it — and reaching
        through the relationship inside a loop is the N+1 that
        ``test_n_plus_one`` exists to catch. Falls back to the relationship when
        called with nothing, so the property is still usable from a shell.

        ``>`` rather than ``!=``: a source version *lower* than the one
        translated from cannot happen through the API, and if it somehow does —
        a restore that rewound the counter, a hand-run UPDATE — the honest
        reading is "we do not know", and the safe answer is the one that holds
        the translation back rather than the one that publishes it.
        """
        source = content if content is not None else self.content
        return source.version > self.source_version

    @property
    def is_publishable(self) -> bool:
        """Whether this may go out without a human reading it first.

        Staleness is deliberately *not* part of this. It needs the piece to
        answer and this property does not take one, and more importantly the two
        refusals are different: a stale translation is one the user can fix with
        a button, and a translation with quality issues is one they have to
        read. The publish path checks both — see
        :func:`app.services.translation.for_publishing`.
        """
        return self.status == TranslationStatus.READY and not self.quality_issues

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<ContentTranslation content={self.content_id} "
            f"language={self.language!r} status={self.status}>"
        )
