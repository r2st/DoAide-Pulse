"""Revision request and response models.

Two shapes rather than one, and the split is the interesting part. A history
*list* must not carry bodies: fifty revisions of a 200,000-character piece is a
ten-megabyte response to render a sidebar of timestamps. So :class:`RevisionOut`
carries the metadata and a word count, and :class:`RevisionDetail` — one row,
asked for by number — carries the text.

That is the same mistake ``herald-payload-width-perf-bug`` records: the query
count is fine either way and the bytes are what hurt, which no query-count budget
test can see. ``test_list_items_are_bounded`` is the sweep that would have caught
it here.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.revision import RevisionSource


class RevisionOut(BaseModel):
    """One entry in a piece's history, without its text."""

    model_config = ConfigDict(from_attributes=True)

    #: The piece's ``version`` when this text was current, and the number the
    #: detail and restore endpoints take. Not this row's primary key: a client
    #: holding ``ETag: "4"`` asks for revision 4 and there is nothing to
    #: translate between.
    revision: int
    source: RevisionSource
    #: Which fields the write that followed this snapshot changed, comma
    #: separated — "title, body_markdown". Written by
    #: :func:`app.services.revisions.snapshot_if_changing`.
    note: str = ""
    word_count: int = 0
    #: ``None`` for a revision written by a machine path, or by an account that
    #: has since been deleted. The two are deliberately not distinguished: both
    #: mean "no person to attribute this to".
    author_user_id: int | None = None
    created_at: datetime


class RevisionDetail(RevisionOut):
    """One past version, with the text in it."""

    title: str
    body_markdown: str
    excerpt: str = ""
    meta_description: str = ""
    keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    focus_keyword: str = ""


class RevisionListOut(BaseModel):
    """A page of history, with the total behind it."""

    items: list[RevisionOut] = Field(default_factory=list)
    #: Every revision stored for this piece, not just this page — the sidebar
    #: says "12 versions" and pages through them ten at a time.
    total: int = 0
    #: What :data:`app.services.revisions.RETENTION` is set to, so a client can
    #: say "older versions are not kept" rather than leaving a user to wonder
    #: where last month went.
    retained: int = 0


class FieldDiffOut(BaseModel):
    """How one field differs between two versions."""

    field: str
    before: str
    after: str
    #: Unified-diff lines for the fields that have lines. Empty for short
    #: scalars, where before/after side by side reads better than a patch.
    unified: list[str] = Field(default_factory=list)
    changed: bool = True


class RevisionDiffOut(BaseModel):
    """The difference between two versions of a piece."""

    #: The older side. Always a stored revision number.
    from_revision: int
    #: The newer side, or ``None`` when it is the piece as it stands now —
    #: which is the comparison the UI asks for by default, and the reason the
    #: current text is deliberately not duplicated into the revisions table.
    to_revision: int | None = None
    fields: list[FieldDiffOut] = Field(default_factory=list)

    @property
    def is_identical(self) -> bool:
        """Whether the two sides are the same text in every tracked field."""
        return not self.fields


class RevisionRestoreOut(BaseModel):
    """The result of putting an earlier version back."""

    #: The version whose text is now current.
    restored_revision: int
    #: The snapshot of what was there a moment ago, so the UI can offer "undo".
    #: A restore is an edit like any other and is itself reversible; this is the
    #: number to click to reverse it.
    previous_revision: int
    #: The piece's new ``version``, which is also its new ``ETag``. Returned so
    #: an editor that restores can keep saving without a reload — without it the
    #: next PATCH carries a stale ``If-Match`` and is refused with a 412.
    version: int
