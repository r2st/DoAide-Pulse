"""Multi-language content: the language registry, and a piece's translations."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import RowId, get_current_user
from app.models.content import Content
from app.models.translation import ContentTranslation
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.schemas.translation import (
    LanguageOut,
    TranslationDetail,
    TranslationListOut,
    TranslationOut,
    TranslationRequestIn,
)
from app.services import languages, translation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["translations"])

#: Budget for asking for a translation.
#:
#: Per account rather than per IP, and tight, because this is the most expensive
#: single request in the API: one model call sized to a whole article, with an
#: output budget 1.6× the input. Ten an hour is more than any human workflow
#: needs — a piece has a handful of target languages — and far less than a loop
#: retrying a failing translation costs. Same reasoning and the same key as
#: every other endpoint that reaches an LLM; see ``test_account_rate_limits``,
#: which sweeps for exactly this and fails when an outbound-calling endpoint has
#: no limiter on it.
TRANSLATE_LIMIT = "10/hour"


def _owned(content_id: RowId, db: Session, user: User) -> Content:
    """The piece, or a 404 whether it is missing or somebody else's."""
    content = db.get(Content, content_id)
    if content is None or content.project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


def _to_out(row: ContentTranslation, content: Content) -> TranslationOut:
    """One translation as the list renders it.

    Takes the piece as well as the row because ``stale`` is derived from both,
    and derived rather than stored so it cannot be wrong. Passing the piece
    explicitly is deliberate friction — see :mod:`app.schemas.translation`.
    """
    entry = languages.get(row.language)
    return TranslationOut(
        language=row.language,
        name=entry.name if entry else row.language,
        endonym=entry.endonym if entry else row.language,
        rtl=bool(entry and entry.rtl),
        status=row.status,
        quality_issues=list(row.quality_issues or []),
        error=row.error,
        stale=row.is_stale(content),
        publishable=row.is_publishable and not row.is_stale(content),
        translated_at=row.translated_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/languages",
    response_model=list[LanguageOut],
    summary="Languages Herald translates into",
    responses=errors(*AUTHENTICATED),
)
def list_languages(
    user: User = Depends(get_current_user),
) -> list[LanguageOut]:
    """Every language a piece can be translated into, including the source.

    Served from the registry the translator and both text-hygiene gates read, so
    a picker cannot offer a language the validator does not know how to check.
    That is not a hypothetical tidiness argument: a language missing from
    :mod:`app.services.languages` gets the English loanword list applied to it,
    and every accented word in a correct translation comes back reported as
    corruption.

    Top-level rather than under ``/content`` because it is a static registry
    rather than a fact about any one piece — and because ``/content/languages``
    would be shadowed by ``/content/{content_id}``.
    """
    return [LanguageOut(**entry.as_dict()) for entry in languages.LANGUAGES]


@router.get(
    "/content/{content_id}/translations",
    response_model=TranslationListOut,
    summary="A piece's translations",
    responses=errors(*OWNED),
)
def list_translations(
    content_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TranslationListOut:
    """Every language this piece exists in, without the bodies.

    Bounded by the size of the registry rather than by a page size, which is why
    this is the one listing in the API with no ``limit``: a piece cannot have
    more translations than there are languages, and the schema refuses a
    language that is not one.

    ``stale`` on each entry is computed against the piece's current ``version``,
    so a translation made before an edit is reported as out of date the moment
    the edit lands, with nothing needing to have run in between.
    """
    content = _owned(content_id, db, user)
    return TranslationListOut(
        items=[_to_out(row, content) for row in translation.listing(db, content)],
        source_language=languages.SOURCE_LANGUAGE,
    )


@router.get(
    "/content/{content_id}/translations/{language}",
    response_model=TranslationDetail,
    summary="One translation, with its text",
    responses=errors(*OWNED),
)
def get_translation(
    content_id: RowId,
    language: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TranslationDetail:
    """One translation of this piece, in full.

    ``language`` accepts a regional tag and resolves it to its base, so a client
    passing the browser's own ``fr-CA`` finds the French row rather than a 404
    it would have no way to interpret.
    """
    content = _owned(content_id, db, user)
    row = translation.get(db, content, language)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"This piece has no {language} translation.",
        )
    base = _to_out(row, content)
    return TranslationDetail(
        **base.model_dump(),
        title=row.title,
        body_markdown=row.body_markdown,
        excerpt=row.excerpt,
        meta_description=row.meta_description,
        generated_by_provider=row.generated_by_provider,
        generated_by_model=row.generated_by_model,
    )


@router.post(
    "/content/{content_id}/translations",
    response_model=TranslationDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Translate a piece",
    responses=errors(
        *OWNED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
@limiter.limit(TRANSLATE_LIMIT, key_func=account_key)
def translate_content(
    request: Request,
    response: Response,
    content_id: RowId,
    payload: TranslationRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TranslationDetail:
    """Translate this piece into one language, replacing any existing version.

    Synchronous, and it takes as long as a generation does. That is a deliberate
    choice over a background job: the user is looking at the piece, the result is
    something they have to read before they trust it, and a queue would mean
    building a second place to find out what happened to it. The rate limit is
    what keeps that honest.

    Re-translating replaces the row rather than adding a candidate, so "the
    French version" always names one thing. A failed retry leaves the previous
    text in place — losing a working translation to a provider outage would make
    the retry button one nobody presses.

    The result is stored whether or not it validates. A translation carrying
    quality issues comes back as ``needs_review``: it is worth a human's five
    minutes and cannot publish unattended. See
    :func:`app.services.translation.validate` for what is checked and — just as
    importantly — what is not. Nothing here judges whether the French is *good*
    French; Herald has no way to know that, and a model asked to grade its own
    output says yes.

    422 for a piece too short or too long to translate and for a language Herald
    does not support; 503 when no provider answered, which is worth retrying.
    """
    content = _owned(content_id, db, user)
    try:
        row = translation.translate(db, content, payload.language)
    except translation.TranslationUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except translation.TranslationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    base = _to_out(row, content)
    return TranslationDetail(
        **base.model_dump(),
        title=row.title,
        body_markdown=row.body_markdown,
        excerpt=row.excerpt,
        meta_description=row.meta_description,
        generated_by_provider=row.generated_by_provider,
        generated_by_model=row.generated_by_model,
    )


@router.delete(
    "/content/{content_id}/translations/{language}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a translation",
    responses=errors(*OWNED),
)
def delete_translation(
    content_id: RowId,
    language: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Delete this piece's translation into one language.

    The way to stop a destination publishing in that language without changing
    the destination: :func:`app.services.translation.for_publishing` falls back
    to the original when there is no row, and says so in the log line.

    Nothing already published changes. A translation that has gone out is on the
    platform, and deleting the row here is Herald forgetting it rather than the
    post coming down — the same rule ``DELETE /content/{id}`` states.
    """
    content = _owned(content_id, db, user)
    row = translation.get(db, content, language)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"This piece has no {language} translation.",
        )
    db.delete(row)
    db.commit()
