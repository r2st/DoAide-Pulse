"""Content templates: the shape of a piece, written once and reused.

Pulse generates prose, which is the right default and the wrong answer for a
whole class of content. A weekly changelog, a release note, a "we're at the
conference" post — these have a fixed shape the author already knows, and asking
a model to reinvent it every Friday produces a piece that is subtly different
each time and needs reading before it goes out. What the author wants is their
own words with the variable parts filled in.

So a template is a body with ``{{placeholders}}`` and a declared list of the
variables that fill them, and it runs in one of two modes:

``literal``
    The rendered text *is* the piece. No model is called: it is deterministic,
    instant, free, and produces exactly what the author wrote. This is the mode
    that makes a changelog a solved problem rather than a supervised one.
``prompt``
    The rendered text becomes the brief handed to the model. The author is
    templating their *instructions* — "write about {{feature}} for
    {{audience}}, mention {{link}}" — which keeps the prose fresh while the
    angle stays consistent.

Variables come from three places, resolved in that order of specificity: the
values supplied at render time, the variable's own default, and a set of
built-ins that are always available (see :mod:`app.services.templates`). The
built-ins are why a template is worth having with a trigger attached — a feed
entry's headline and link land in ``{{signal.headline}}`` and ``{{signal.url}}``
with nobody typing anything.

Owned by the user rather than by a project: the reason to write a template is to
use it more than once, and a per-project template would have to be copied to be
reused. A template may still *name* a project as its usual target, which is only
a default for the picker.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from enum import Enum
from typing import TYPE_CHECKING, Any

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
from app.models.content import ContentType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class TemplateMode(str, Enum):
    """Whether the rendered text is the piece or the brief. See the docstring."""

    LITERAL = "literal"
    PROMPT = "prompt"

    @property
    def label(self) -> str:
        """What the mode does, in the words the editor shows next to the toggle."""
        return {
            "literal": "Use it as written",
            "prompt": "Brief the model with it",
        }[self.value]


class ContentTemplate(Base, TimestampMixin):
    """One reusable shape for a piece of content."""

    __tablename__ = "content_templates"
    __table_args__ = (
        # Templates are picked from a list by name, so two called "Weekly
        # changelog" is a usability bug rather than a data one — but it is one
        # the database can simply prevent.
        UniqueConstraint("user_id", "name", name="uq_template_user_name"),
        Index("ix_template_user_updated", "user_id", "updated_at"),
        CheckConstraint("use_count >= 0", name="ck_template_use_count_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    mode: Mapped[TemplateMode] = mapped_column(
        SAEnum(TemplateMode, native_enum=False, length=20),
        default=TemplateMode.LITERAL,
        nullable=False,
    )
    content_type: Mapped[ContentType] = mapped_column(
        SAEnum(ContentType, native_enum=False, length=30),
        default=ContentType.ANNOUNCEMENT,
        nullable=False,
    )

    #: Templated too, and separately: the headline is the part most worth
    #: keeping consistent across a series, and it is the one field a `prompt`
    #: template still wants to control literally.
    title_template: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    body_template: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The declared variables, each ``{"name", "label", "description",
    #: "default", "required"}``. A list rather than a dict because the order is
    #: the order the form asks for them in, and that is the author's choice.
    #: Validated on write by ``app.schemas.template``; read defensively here.
    variables: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    #: The project this template is usually for. Only a default for the picker —
    #: a template is owned by the user precisely so it can be used anywhere.
    #: Not a cascade: deleting a project should not delete the author's writing.
    #:
    #: Indexed for two separate reasons, either of which would be enough. It is
    #: a *filter* — ``GET /templates?project_id=`` narrows on this column
    #: directly — and it is the referencing side of an ``ON DELETE SET NULL``,
    #: which Postgres enforces by finding the rows that point at the project
    #: being deleted. Unindexed, both of those read the whole table, and the
    #: second one does it inside the delete's transaction.
    default_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )

    #: How many pieces this template has produced. The only honest answer to
    #: "is this one still earning its place in the list?".
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="templates")

    def variable(self, name: str) -> dict[str, Any] | None:
        """One declared variable by name, or None."""
        for entry in self.variables or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        return None

    @property
    def variable_names(self) -> list[str]:
        """Declared variable names, in declaration order.

        Skips malformed entries rather than raising: ``variables`` is a JSON
        column, so a row written by an older version — or by hand — can hold
        anything, and the rendering path already reports an unresolved
        placeholder as a missing variable.
        """
        return [
            str(entry["name"])
            for entry in (self.variables or [])
            if isinstance(entry, dict) and entry.get("name")
        ]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ContentTemplate id={self.id} name={self.name!r} mode={self.mode}>"


__all__ = ["ContentTemplate", "TemplateMode"]
