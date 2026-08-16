"""Translation request and response models.

The same list/detail split as :mod:`app.schemas.revision`, for the same reason:
a language picker showing eight flags must not download eight bodies to do it.

The one thing worth reading closely is :class:`TranslationOut.stale`. It is
computed against the piece rather than stored, so it cannot be wrong — see
:meth:`app.models.translation.ContentTranslation.is_stale` — which means the
router has to pass the piece in. That is deliberate friction: a field that
silently defaults to "fresh" when the caller forgets is a field that publishes
an out-of-date translation.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.translation import TranslationStatus
from app.services import languages


class LanguageOut(BaseModel):
    """One language Herald can translate into."""

    code: str
    #: In English, for the picker's label.
    name: str
    #: In the language itself — ``Deutsch``, not ``German``. What a reader of it
    #: expects to see on a switcher.
    endonym: str
    script: str
    #: Written right to left. The client sets ``dir`` from this; nothing
    #: server-side validates against it.
    rtl: bool = False


class TranslationRequestIn(BaseModel):
    """Ask for a piece to be translated into one language."""

    language: str = Field(
        description=(
            "A language code from ``GET /content/languages``. Regional tags are "
            "accepted and reduced to their base — ``fr-CA`` is French."
        ),
        max_length=16,
    )

    @field_validator("language")
    @classmethod
    def _supported(cls, value: str) -> str:
        """Reject an unsupported language here, before anything is written.

        The alternative is a row in ``pending`` for a language that will never
        arrive, and a bill for the model call that discovers it. Normalised on
        the way through so the stored value is always a base tag — the column is
        part of a unique constraint, and ``fr`` and ``fr-CA`` must not be able to
        become two rows for one language.
        """
        code = languages.normalize(value)
        if code is None:
            raise ValueError(
                f"{value!r} is not a supported language. See GET /content/languages."
            )
        if code == languages.SOURCE_LANGUAGE:
            raise ValueError("Pieces are already written in English.")
        return code


class QualityIssueOut(BaseModel):
    """One structural problem found in a translation."""

    #: Stable identifier — ``too_short``, ``code_blocks_changed``. The UI maps
    #: it to an explanation; the message is the fallback.
    code: str
    message: str


class TranslationOut(BaseModel):
    """One translation, without its body."""

    language: str
    #: The language's own name, so a picker need not join against
    #: ``GET /content/languages`` to render one row.
    name: str = ""
    endonym: str = ""
    rtl: bool = False
    status: TranslationStatus
    #: Empty is the good case. A non-empty list means a human has to read the
    #: translation before it can go out.
    quality_issues: list[QualityIssueOut] = Field(default_factory=list)
    #: Why the last attempt failed, when it did.
    error: str = ""
    #: Whether the piece has been edited since this was translated. Computed,
    #: never stored.
    stale: bool = False
    #: Whether this would actually be used if the piece published now: ready,
    #: no issues, not stale. The three refusals have different fixes, so they
    #: are reported separately above as well.
    publishable: bool = False
    translated_at: datetime | None = None
    updated_at: datetime | None = None


class TranslationDetail(TranslationOut):
    """One translation, with the text in it."""

    title: str = ""
    body_markdown: str = ""
    excerpt: str = ""
    meta_description: str = ""
    generated_by_provider: str | None = None
    generated_by_model: str | None = None


class TranslationListOut(BaseModel):
    """Every translation of one piece."""

    items: list[TranslationOut] = Field(default_factory=list)
    #: The piece's own language. Named rather than assumed, because the client
    #: renders it as the first entry of the switcher and should not hard-code
    #: which one that is.
    source_language: str = languages.SOURCE_LANGUAGE
