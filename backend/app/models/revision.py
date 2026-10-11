"""Past versions of a piece, so an edit is something you can undo.

``Content.version`` has been a real ``version_id_col`` since the two-editors
work: it counts writes, and the PATCH route hands it back as an ``ETag`` so a
save landing on top of somebody else's is refused. What it has never been is a
*history*. The counter went from 4 to 5 and the text that was version 4 is gone
— which makes "revert to what it said this morning" a feature request rather
than an edge case, and makes the conflict machinery a strictly worse deal than
it looks: Pulse can tell you somebody overwrote your paragraph, and cannot show
you what it said.

A revision is a snapshot of the fields a human edits, taken *before* the write
that replaces them. Before, not after, because the interesting question is
always "what did it say" rather than "what does it say" — the second is on the
``content`` row already. So revision *n* holds the text as of ``Content.version
= n``, and the piece's current text is never duplicated here. A piece written
once and never edited has no revisions at all, which is the honest answer: there
is nothing to go back to.

**What is snapshotted, and what is not.** Only the fields an author writes:
title, body, excerpt, meta description, tags, keywords, focus keyword. Not
``status``, not ``scheduled_for``, not the publication rows. Restoring a
revision must not un-publish a piece or re-arm a queue that has already run, and
the cleanest way to guarantee that is for the history to have never held those
columns in the first place. See :func:`app.services.revisions.restore`.

**Why the snapshot is not a diff.** Storing whole bodies is more bytes than
storing patches, and every other property is better: a row can be read without
replaying the ones before it, a corrupted row costs one revision rather than all
of them after it, and the retention sweep can drop the oldest without rewriting
the youngest. Pulse's bodies are a few kilobytes and capped at
:data:`app.models.content.BODY_MARKDOWN_MAX_LENGTH`; the patch-chain version of
this trades a real operational hazard for a saving nobody would notice.

**Kept bounded, like everything else that grows per write.** See
:data:`app.services.revisions.RETENTION`.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
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
from app.models.content import (
    META_DESCRIPTION_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.content import Content


class RevisionSource(StrEnum):
    """What produced the write that this revision was taken ahead of.

    A history is worth much more when each entry says who moved it, because the
    question a user actually asks is "when did my subheading disappear" and the
    answer is usually a machine. Pulse writes a piece from several directions —
    a person in the editor, the repurposer, a restore — and telling them apart
    is the difference between a list of timestamps and an account of what
    happened.
    """

    #: A person editing through the API. The common case.
    EDIT = "edit"
    #: Restoring an older revision, which is itself an edit and gets a snapshot
    #: of its own so a restore can be undone. See :func:`revisions.restore`.
    RESTORE = "restore"
    #: A model rewrote the piece: the headline applier, a regeneration.
    GENERATION = "generation"
    #: Anything that writes the columns without a person or a model deciding to
    #: — a migration, a backfill, a fix run by hand during an incident.
    SYSTEM = "system"

    @property
    def label(self) -> str:
        """What this source did, in the words the history list shows."""
        return {
            "edit": "Edited",
            "restore": "Restored an earlier version",
            "generation": "Rewritten by Pulse",
            "system": "Changed by Pulse",
        }[self.value]


class ContentRevision(Base, TimestampMixin):
    """One past version of a piece's editable fields."""

    __tablename__ = "content_revisions"
    __table_args__ = (
        # The pair a caller names a revision by. Unique because ``revision`` is
        # allocated per piece rather than globally — a user asking for "version
        # 3" of this piece means the third write of *this* piece — and because
        # the uniqueness is what makes two concurrent snapshots of the same
        # piece an ``IntegrityError`` to retry rather than two rows both calling
        # themselves version 3. See :func:`app.services.revisions.snapshot`.
        UniqueConstraint("content_id", "revision", name="uq_revision_per_content"),
        # The history list, newest first, for one piece. Descending on
        # ``revision`` rather than on ``created_at``: they order identically
        # today, and only one of them is guaranteed to — two snapshots taken in
        # the same millisecond by a bulk edit tie on the timestamp, and the
        # counter never ties.
        Index("ix_revision_content_newest", "content_id", "revision"),
        CheckConstraint("revision > 0", name="ck_revision_positive"),
        CheckConstraint("word_count >= 0", name="ck_revision_word_count_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The value ``Content.version`` held when this text was current. Not a
    #: sequence of this table's own: pinning it to the counter the ``ETag``
    #: already exposes means a client holding ``If-Match: "4"`` can ask for
    #: revision 4 and get exactly the copy it was editing, with no second
    #: numbering for anyone to translate between.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meta_description: Mapped[str] = mapped_column(
        String(META_DESCRIPTION_MAX_LENGTH), default="", nullable=False
    )
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    focus_keyword: Mapped[str] = mapped_column(String(100), default="", nullable=False)

    #: Denormalised from the body at snapshot time. The history list shows "1,240
    #: words → 1,890 words" beside each entry, and computing it per row on read
    #: means counting every word of every revision of the piece to render one
    #: list. ``Content.word_count`` exists for the same reason.
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    source: Mapped[RevisionSource] = mapped_column(
        SAEnum(RevisionSource, native_enum=False, length=20),
        default=RevisionSource.EDIT,
        nullable=False,
    )
    #: Who made the write this snapshot was taken ahead of. Nullable, and stays
    #: nullable: a machine path has no user, and ``ON DELETE SET NULL`` rather
    #: than cascade because losing the account should not silently rewrite the
    #: history of a piece that outlived it.
    #:
    #: Indexed because ``ON DELETE SET NULL`` is a write: deleting an account
    #: makes the database find every revision that account authored, across
    #: every piece in the fleet, and without an index that is a full scan of the
    #: largest table this feature adds.
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: One line of context, when the caller has one — "restored version 3",
    #: "applied headline variant B". Free text, bounded, never shown as HTML.
    note: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    content: Mapped[Content] = relationship(back_populates="revisions")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ContentRevision content={self.content_id} revision={self.revision}>"
