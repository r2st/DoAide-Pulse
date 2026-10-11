"""Template request and response models.

The whole point of validating here rather than at render time is stated in
:mod:`app.services.templates`: a placeholder typo must be an error in the editor
that produced it, not a literal ``{{versoin}}`` in a published post three days
later. So every write runs the same three checks —

* every ``{{placeholder}}`` is either declared or built in,
* no declared variable shadows a built-in namespace,
* no two variables share a name —

and returns them as a 422 with the offending names in it. The author is looking
at the template when this happens, which is the only moment the message is
cheap to act on.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.content import ContentType
from app.models.template import TemplateMode
from app.schemas.limits import BoundedId
from app.services.templates import (
    BUILTINS,
    reserved_names,
    unknown_placeholders,
)

#: A variable name is a bare Python-style identifier. Dots are reserved for the
#: built-in namespaces, so a declared name may not contain one.
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How many variables one template may declare. A form with more fields than
#: this is a document, not a template.
MAX_VARIABLES = 25

#: How many templates one user may keep. Generous — they cost a row each — but
#: not unbounded, since the picker is a list a human reads.
MAX_TEMPLATES_PER_USER = 100


class TemplateVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    #: What the form calls it. Falls back to the name.
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=300)
    #: Used when the render supplies nothing. A default makes a variable
    #: effectively optional even when it is marked required.
    default: str = Field(default="", max_length=2000)
    required: bool = False

    @field_validator("name")
    @classmethod
    def _identifier(cls, value: str) -> str:
        name = value.strip()
        if not NAME_RE.match(name):
            raise ValueError(
                f"{value!r} is not a usable variable name. Use letters, digits "
                "and underscores, starting with a letter — 'release_version', "
                "not 'release version'."
            )
        return name

    @model_validator(mode="after")
    def _label_defaults_to_name(self) -> TemplateVariable:
        if not self.label.strip():
            self.label = self.name.replace("_", " ").capitalize()
        return self


class TemplateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    mode: TemplateMode = TemplateMode.LITERAL
    content_type: ContentType = ContentType.ANNOUNCEMENT
    title_template: str = Field(default="", max_length=300)
    body_template: str = Field(default="", max_length=50_000)
    variables: list[TemplateVariable] = Field(default_factory=list)
    default_project_id: BoundedId | None = None

    @field_validator("name")
    @classmethod
    def _named(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("A template needs a name — it is how you will find it.")
        return name

    @model_validator(mode="after")
    def _placeholders_resolve(self) -> TemplateBase:
        declared = [v.name for v in self.variables]

        duplicates = sorted({n for n in declared if declared.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Declared twice: {', '.join(duplicates)}. Each variable needs "
                "its own name — the second one would win silently."
            )

        shadowed = reserved_names(declared)
        if shadowed:
            raise ValueError(
                f"{', '.join(shadowed)} clashes with a built-in Pulse fills in "
                f"for you ({', '.join(sorted(BUILTINS))}). Pick another name."
            )

        unknown = unknown_placeholders(
            declared, self.title_template, self.body_template
        )
        if unknown:
            raise ValueError(
                "This template uses "
                + ", ".join(f"{{{{{name}}}}}" for name in unknown)
                + ", which is neither a variable you declared nor one Pulse "
                "fills in. Declare it, or fix the spelling."
            )

        if len(self.variables) > MAX_VARIABLES:
            raise ValueError(f"At most {MAX_VARIABLES} variables per template.")

        return self


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    mode: TemplateMode | None = None
    content_type: ContentType | None = None
    title_template: str | None = Field(default=None, max_length=300)
    body_template: str | None = Field(default=None, max_length=50_000)
    variables: list[TemplateVariable] | None = None
    default_project_id: BoundedId | None = None


class TemplateOut(BaseModel):
    id: int
    name: str
    description: str
    mode: TemplateMode
    mode_label: str
    content_type: ContentType
    title_template: str
    #: The template's text. ``None`` in a listing unless it was asked for —
    #: ``GET /templates`` leaves it out by default, because a body is up to
    #: ``50_000`` characters and a hundred of them is a five-megabyte response
    #: to draw a list of names. ``GET /templates/{id}`` always carries it, and
    #: so does every write, so the editor has one round trip to reach it.
    #:
    #: ``None`` rather than ``""``: an empty string is a template whose body is
    #: genuinely blank, which is a real state a picker should be able to show.
    body_template: str | None = None
    variables: list[TemplateVariable]
    default_project_id: int | None
    use_count: int
    #: Every placeholder the template actually uses, declared or built-in. The
    #: editor highlights against this rather than re-parsing in the browser.
    placeholders_used: list[str]
    created_at: datetime
    updated_at: datetime


class BuiltinOut(BaseModel):
    """One always-available placeholder, for the editor's insert menu."""

    name: str
    description: str


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict)
    #: Which project's facts fill ``{{project.*}}``. Falls back to the
    #: template's default project, then to blanks.
    project_id: BoundedId | None = None


class RenderOut(BaseModel):
    title: str
    body: str
    #: Declared, required, and still empty. A preview renders anyway; a real
    #: piece refuses.
    missing: list[str]
    filled: list[str]
    is_complete: bool
    #: ``"title"``, ``"body"``, or both: what came out longer than a piece may
    #: be and was clipped for this preview. Same contract as ``missing`` —
    #: shown here, refused by ``POST /templates/{id}/use``.
    over_limit: list[str] = []


class TemplateUseRequest(RenderRequest):
    """Turn a filled-in template into an actual draft."""

    #: Overrides the template's own content type for this one piece.
    content_type: ContentType | None = None


__all__ = [
    "MAX_TEMPLATES_PER_USER",
    "MAX_VARIABLES",
    "BuiltinOut",
    "RenderOut",
    "RenderRequest",
    "TemplateCreate",
    "TemplateOut",
    "TemplateUpdate",
    "TemplateUseRequest",
    "TemplateVariable",
]
