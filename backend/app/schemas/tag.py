"""Tag schemas: the tree, the suggestions, and the rename."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.limits import BoundedId, Tag as TagValue
from app.services import tags as tag_service


def _canonical(value: str) -> str:
    """Normalise a tag argument, refusing one that survives as nothing.

    A validator rather than a silent :func:`app.services.tags.normalize` at the
    call site, because these two fields are *arguments*, not stored values. A
    stored list is normalised quietly — dropping ``"!!!"`` from a tag list costs
    the writer nothing they meant. A rename whose ``old`` normalises to the
    empty string is a rename of every tag or of none depending on how the
    prefix match is written, and answering 200 to it either way is the wrong
    end of that question.
    """
    canonical = tag_service.normalize(value)
    if not canonical:
        raise ValueError(
            "not a usable tag — needs at least one letter or digit, and is "
            "written 'parent/child' for a nested one"
        )
    return canonical


class TagNodeOut(BaseModel):
    """One tag in the tree. Recursive: ``children`` are the same shape."""

    tag: str = Field(description="The full path, e.g. 'guides/deployment'.")
    label: str = Field(description="The last level only — what a platform gets.")
    depth: int
    direct: int = Field(description="Pieces carrying this exact tag.")
    total: int = Field(description="Pieces carrying this tag or anything under it.")
    children: list[TagNodeOut] = []


class TagTreeOut(BaseModel):
    """Every tag this account uses, nested, with counts."""

    roots: list[TagNodeOut] = []
    distinct: int = Field(description="Distinct tags found, implied parents included.")
    scanned: int = Field(description="Pieces read to build this.")
    truncated: bool = Field(
        description=(
            "True when the account has more pieces than the scan reads, so "
            "every count is a floor. Plan a rename against a truncated tree "
            "and the preview will under-report."
        )
    )


class TagSuggestionOut(BaseModel):
    """One proposed tag, with why."""

    tag: str
    score: int = Field(
        ge=0,
        le=100,
        description=(
            "Ranking within this response only. Not a probability and not "
            "comparable against another piece's suggestions."
        ),
    )
    reason: str


class TagRenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old: TagValue = Field(description="The tag to move. Its subtree moves with it.")
    new: TagValue = Field(
        description=(
            "Where it lands. An existing tag is a merge rather than an error — "
            "a piece that carried both ends up carrying it once."
        )
    )
    project_id: BoundedId | None = Field(
        default=None, description="Limit the rename to one project's content."
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Report which pieces would change and change nothing. This is the "
            "one write in Pulse that touches every piece carrying a string, "
            "named by that string rather than by id, so the preview is the "
            "point rather than a convenience."
        ),
    )

    _canonical_old = field_validator("old")(_canonical)
    _canonical_new = field_validator("new")(_canonical)


class TagRenameOut(BaseModel):
    """What a rename moved, or would have."""

    old: str = Field(description="The canonical form of what was asked for.")
    new: str
    content_ids: list[int] = Field(
        default=[], description="The pieces changed, or on a dry run, the candidates."
    )
    count: int
    #: Pieces that already carried ``new`` as well as ``old``, so the rename
    #: collapsed two tags into one on them. Called out because it is the one
    #: outcome of a rename that loses information — the row goes from two tags
    #: to one — and a caller who meant "rename" rather than "merge" wants to
    #: know before, which is what ``dry_run`` is for.
    merged: list[int] = []
    dry_run: bool
