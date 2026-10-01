"""Taking, listing, diffing and restoring past versions of a piece.

The model docstring in :mod:`app.models.revision` argues for the shape of the
table. This module owns the four decisions around it:

**When a snapshot is taken.** Before a write, and only when the write actually
changes something — see :func:`snapshot_if_changing`. A PATCH that re-sends the
body it already has is the most common request the editor makes (autosave fires
on a timer, not on a keystroke), and a history where nine entries in ten are
identical is not a history, it is a log with the useful rows hidden in it.

**What "the same" means.** Field by field, on the seven columns a human edits,
comparing what the caller sent against what is stored. Not a hash of the row: a
PATCH carries only the fields it wants to change, so a hash would have to be
taken of a merged copy that does not exist yet, and the merge is the thing being
decided.

**How far back it goes.** :data:`RETENTION` entries per piece, oldest dropped
first. Unbounded history is the version of this feature that works perfectly for
a year and then is a table nobody can migrate: a piece under autopilot with an
hourly headline test writes a revision an hour forever, and the bodies are
kilobytes each. Pulse bounds everything that grows per write — deliveries,
trigger events, preview links, LLM usage all have retention sweeps — and this is
the same rule, applied at write time rather than by a nightly task because the
bound is per piece rather than per fleet and the write already has the piece
in hand.

**What a restore is.** Another edit. It snapshots the current text first, then
applies the old one, so the button is never the destructive one — the state you
just left is revision *n* and one more click brings it back. A restore that
simply overwrote would be the only irreversible operation in a feature whose
entire purpose is reversibility.
"""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.content import Content, unique_content_slug
from app.models.revision import ContentRevision, RevisionSource

logger = logging.getLogger(__name__)

#: How many past versions one piece keeps.
#:
#: Fifty is roughly a working week of a piece being actively edited, and about
#: two days of one under an hourly automated headline test. Past it, the entries
#: being dropped are ones nobody has looked at: the value of a revision falls off
#: a cliff once a newer one exists that is also good, and the case this feature
#: is really for — "undo what just happened" — is served by the first three.
RETENTION = 50

#: The columns a revision captures, and therefore the ones a restore writes.
#:
#: Deliberately not ``status``, ``scheduled_for``, ``slug`` or anything about
#: publications. A restore must not un-publish a piece, re-arm a queue that has
#: already run, or move a URL a reader has bookmarked; keeping those columns out
#: of the snapshot means no future caller can make it do any of that by passing
#: one more field. ``slug`` is the interesting exclusion — it is derived from
#: the title, so a restore that changes the title regenerates it through the
#: same :func:`unique_content_slug` the PATCH route uses, rather than restoring
#: a stored value that may since have been taken by another piece.
TRACKED_FIELDS: tuple[str, ...] = (
    "title",
    "body_markdown",
    "excerpt",
    "meta_description",
    "keywords",
    "tags",
    "focus_keyword",
)


class RevisionError(ValueError):
    """The revision cannot be taken or restored. The message is user-facing."""


def _values(content: Content) -> dict[str, Any]:
    """The tracked fields of *content*, as a plain dict.

    Lists are copied rather than referenced. A ``tags`` list handed straight to
    the revision row is the *same object* the content row holds, so the PATCH's
    ``setattr`` loop would mutate the snapshot it just took — and because
    SQLAlchemy's JSON columns compare by value on flush, the corruption would be
    written out with no error anywhere.
    """
    return {
        field: (list(value) if isinstance(value, list) else value)
        for field, value in ((name, getattr(content, name)) for name in TRACKED_FIELDS)
    }


def changed_fields(content: Content, incoming: dict[str, Any]) -> list[str]:
    """Which tracked fields *incoming* would actually change on *content*.

    Only keys present in *incoming* are considered, because a PATCH says nothing
    about the fields it omits. Keys outside :data:`TRACKED_FIELDS` are ignored
    entirely: a request that changes only ``status`` or ``scheduled_for`` has
    not touched the text, and giving it a revision would put an entry in the
    history that renders as "no changes".
    """
    changed = []
    for field in TRACKED_FIELDS:
        if field not in incoming:
            continue
        if incoming[field] != getattr(content, field):
            changed.append(field)
    return changed


def snapshot(
    db: Session,
    content: Content,
    *,
    source: RevisionSource = RevisionSource.EDIT,
    author_user_id: int | None = None,
    note: str = "",
) -> ContentRevision:
    """Record *content*'s current text as revision ``content.version``.

    Added to the session and pruned, but **not committed**: the caller is in the
    middle of a write and the snapshot belongs to the same transaction as the
    write it precedes. A snapshot that committed separately would survive a
    request that then failed its own validation, leaving a history entry for an
    edit that never happened.

    Idempotent per version. Calling it twice before a flush — two code paths in
    one request both deciding to be careful — returns the row already staged
    rather than tripping ``uq_revision_per_content``, because the pair
    (piece, version) names one text and it does not matter who asked for it
    first.
    """
    existing = _staged(db, content)
    if existing is not None:
        return existing

    revision = ContentRevision(
        content_id=content.id,
        revision=content.version,
        word_count=content.word_count,
        source=source,
        author_user_id=author_user_id,
        note=note[:200],
        **_values(content),
    )
    db.add(revision)
    _prune(db, content)
    return revision


def _staged(db: Session, content: Content) -> ContentRevision | None:
    """A revision for this piece's current version already in the session.

    Reads ``db.new`` rather than querying: the row this guards against has not
    been flushed, so a SELECT would not see it, and flushing to find out would
    push the caller's half-applied write to the database early.
    """
    for obj in db.new:
        if (
            isinstance(obj, ContentRevision)
            and obj.content_id == content.id
            and obj.revision == content.version
        ):
            return obj
    return None


def _prune(db: Session, content: Content) -> None:
    """Drop the oldest revisions past :data:`RETENTION` for this piece.

    Counts what is stored and deletes down to ``RETENTION - 1``, leaving room
    for the row being added in the same transaction. Two statements rather than
    a subquery with ``OFFSET``, because MySQL forbids that shape and Pulse's
    two backends should not diverge on which one runs the cheaper plan.

    Deleted with a bulk statement, so the ORM never loads fifty bodies to throw
    them away — the whole point of a retention sweep is that it does not cost
    what it is cleaning up.
    """
    stored = db.scalar(
        select(func.count(ContentRevision.id)).where(ContentRevision.content_id == content.id)
    )
    surplus = (stored or 0) - (RETENTION - 1)
    if surplus <= 0:
        return

    doomed = db.scalars(
        select(ContentRevision.id)
        .where(ContentRevision.content_id == content.id)
        .order_by(ContentRevision.revision.asc())
        .limit(surplus)
    ).all()
    if doomed:
        db.execute(delete(ContentRevision).where(ContentRevision.id.in_(doomed)))


def snapshot_if_changing(
    db: Session,
    content: Content,
    incoming: dict[str, Any],
    *,
    source: RevisionSource = RevisionSource.EDIT,
    author_user_id: int | None = None,
) -> ContentRevision | None:
    """Snapshot *content* only if *incoming* would change its text.

    The entry point the PATCH route uses. The note it writes names the fields
    that moved, which is what turns the history list from a column of timestamps
    into something a user can scan: "Edited — title, body" answers "was that the
    change that lost my subheading" without opening the diff.
    """
    changed = changed_fields(content, incoming)
    if not changed:
        return None
    return snapshot(
        db,
        content,
        source=source,
        author_user_id=author_user_id,
        note=", ".join(changed),
    )


def history(
    db: Session, content: Content, *, limit: int = 20, offset: int = 0
) -> list[ContentRevision]:
    """This piece's past versions, newest first.

    Bodies are deferred nowhere and loaded whole, which is the right trade at
    ``limit`` of twenty: the list screen shows a word count and a note per row,
    but the diff the user clicks next needs the body, and a second round trip
    per row to fetch it is the N+1 this would be criticised for.
    """
    stmt = (
        select(ContentRevision)
        .where(ContentRevision.content_id == content.id)
        .order_by(ContentRevision.revision.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.scalars(stmt))


def count(db: Session, content: Content) -> int:
    """How many past versions this piece has, for the list's total."""
    return (
        db.scalar(
            select(func.count(ContentRevision.id)).where(
                ContentRevision.content_id == content.id
            )
        )
        or 0
    )


def get(db: Session, content: Content, revision: int) -> ContentRevision | None:
    """One past version by its number, or ``None``.

    Scoped to the piece in the WHERE clause rather than fetched by primary key
    and checked afterwards, so a revision number belonging to somebody else's
    piece is simply absent — the same shape every ownership check in the tree
    uses, and the reason this one cannot leak a body across accounts.
    """
    return db.scalar(
        select(ContentRevision).where(
            ContentRevision.content_id == content.id,
            ContentRevision.revision == revision,
        )
    )


@dataclass(frozen=True)
class FieldDiff:
    """How one tracked field differs between two versions."""

    field: str
    before: str
    after: str
    #: Unified-diff lines, for the fields worth showing line by line. Empty for
    #: short scalar fields, where "before" and "after" side by side is clearer
    #: than a two-line patch with an ``@@`` header over it.
    unified: list[str]

    @property
    def changed(self) -> bool:
        """Whether the two sides differ at all."""
        return self.before != self.after

    def as_dict(self) -> dict[str, Any]:
        """The diff as the API returns it."""
        return {
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "unified": self.unified,
            "changed": self.changed,
        }


#: Fields rendered as a unified diff rather than as two whole values. The line
#: is drawn at "does this have lines in it": a body does, a focus keyword does
#: not, and a patch header over a thirty-character string is noise.
_LINE_DIFFED = frozenset({"body_markdown", "excerpt"})

#: Cap on the unified diff handed back for one field. A body may be 200,000
#: characters and a diff of two unrelated ones is every line of both; past this
#: the response has stopped being something a reviewer reads and started being
#: something a browser struggles to render. The caller is told it was clipped
#: rather than being handed a truncation that looks like the whole answer.
DIFF_LINE_LIMIT = 400


def _as_text(value: Any) -> str:
    """One tracked field as comparable text.

    ``tags`` and ``keywords`` are JSON lists; rendering them comma-separated
    compares them the way a person reads them, and means a reordering shows up
    as a change — which it is, because the first tag is the one several adapters
    treat as primary.
    """
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def diff(before: ContentRevision | Content, after: ContentRevision | Content) -> list[FieldDiff]:
    """How the two versions differ, field by field, changed fields only.

    Takes either a revision or the live piece on both sides, because the
    comparison a user asks for most is "this old version against what it says
    now" and the current text is deliberately not in the revisions table. Both
    types carry the same seven attributes, which is what makes this work without
    a conversion step — and is the reason :data:`TRACKED_FIELDS` is defined once
    and imported rather than being spelled out in the model.
    """
    diffs = []
    for field in TRACKED_FIELDS:
        left = _as_text(getattr(before, field, ""))
        right = _as_text(getattr(after, field, ""))
        if left == right:
            continue
        unified: list[str] = []
        if field in _LINE_DIFFED:
            unified = list(
                difflib.unified_diff(
                    left.splitlines(),
                    right.splitlines(),
                    lineterm="",
                    n=3,
                )
            )
            if len(unified) > DIFF_LINE_LIMIT:
                clipped = len(unified) - DIFF_LINE_LIMIT
                unified = unified[:DIFF_LINE_LIMIT]
                unified.append(f"… {clipped} more line(s) not shown")
        diffs.append(FieldDiff(field=field, before=left, after=right, unified=unified))
    return diffs


def restore(
    db: Session,
    content: Content,
    revision: ContentRevision,
    *,
    author_user_id: int | None = None,
) -> ContentRevision:
    """Put *revision*'s text back, keeping the text it replaces.

    Returns the snapshot of what was there before, so the caller can tell the
    user which version to click to undo this.

    Refuses on a piece that went out, in the same words and for the same
    reason the PATCH route refuses to edit one: the text is live on the
    platforms, and changing it here would make Pulse disagree with what a
    reader can see without changing anything a reader can see. Asked of the
    rows rather than the column (:attr:`Content.went_out`), because archiving
    moves the column and leaves the post up — and a restore on an archived
    piece that had been published rewrote the body, the title and the slug of
    a live post, which is the two-call edit the PATCH's freeze was closed
    against. An archived piece that never went out still restores: archiving
    means "stop showing me this", not "this went out".

    Not committed, again: the caller commits, so a restore and the response it
    builds succeed or fail together.
    """
    if content.went_out:
        raise RevisionError(
            "This piece is already published. Restoring an earlier version here "
            "would not change what is live on the platforms."
        )

    previous = snapshot(
        db,
        content,
        source=RevisionSource.RESTORE,
        author_user_id=author_user_id,
        note=f"restored version {revision.revision}",
    )

    if revision.title != content.title:
        # Same rule as the PATCH route: the slug follows the title, and is
        # recomputed rather than restored. A stored slug may since have been
        # taken by another piece in this project, and the unique index would
        # answer that with an IntegrityError at commit rather than a suffix.
        content.slug = unique_content_slug(db, content.project_id, revision.title)

    for field in TRACKED_FIELDS:
        value = getattr(revision, field)
        setattr(content, field, list(value) if isinstance(value, list) else value)

    logger.info(
        "restored content %s to revision %s (previous banked as %s)",
        content.id,
        revision.revision,
        previous.revision,
    )
    return previous
