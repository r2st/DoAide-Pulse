"""Template CRUD, preview, and the endpoint that turns one into a draft.

Two of these are worth reading twice.

``POST /templates/{id}/preview`` renders without saving anything, and renders
even when required variables are empty. A preview whose job is to show the
author what their template does is more useful half-filled than replaced by an
error message — the missing names come back in the response, and the editor
shows them next to the fields that are blank.

``POST /templates/{id}/use`` refuses on the same condition, because that one
writes a row. It is also where the two modes diverge and where the whole design
pays off: a ``literal`` template produces a finished draft with no model call at
all — deterministic, instant, free — while a ``prompt`` template hands its
rendered text to the generator as the brief. Same editor, same variables, and
the author picks per template whether they are writing the post or the
instructions.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import ListOffset, RowId, get_current_user, owned_project
from app.models.content import Content, ContentStatus, unique_content_slug
from app.models.project import Project
from app.models.template import ContentTemplate, TemplateMode
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.content import ContentDetail
from app.schemas.errors import AUTHENTICATED, OWNED, errors
from app.schemas.template import (
    MAX_TEMPLATES_PER_USER,
    BuiltinOut,
    RenderOut,
    RenderRequest,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
    TemplateUseRequest,
    TemplateVariable,
)
from app.services import content_generator, seo
from app.services import templates as template_service
from app.services.templates import BUILTINS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])


def _owned(template_id: RowId, db: Session, user: User) -> ContentTemplate:
    """Fetch a template, 404ing if it isn't this user's.

    Same status for missing and forbidden, for the reason in ``deps``.
    """
    template = db.get(ContentTemplate, template_id)
    if template is None or template.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Template not found"
        )
    return template


def _to_out(template: ContentTemplate, *, body: bool = True) -> TemplateOut:
    """One template on the wire. *body* carries :attr:`body_template`.

    Every single-template response passes ``True``; the listing passes whatever
    the caller asked for. ``placeholders_used`` is computed either way — it is
    the one thing the cards render that is derived from the body, and it is a
    handful of names rather than the text they were found in.
    """
    mode = (
        template.mode
        if isinstance(template.mode, TemplateMode)
        else TemplateMode(template.mode)
    )
    return TemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        mode=mode,
        mode_label=mode.label,
        content_type=template.content_type,
        title_template=template.title_template,
        body_template=template.body_template if body else None,
        variables=[TemplateVariable(**v) for v in (template.variables or [])],
        default_project_id=template.default_project_id,
        use_count=template.use_count,
        placeholders_used=template_service.placeholders(
            template.title_template or "", template.body_template or ""
        ),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _resolve_project(
    template: ContentTemplate,
    project_id: int | None,
    db: Session,
    user: User,
) -> Project | None:
    """Which project's facts fill ``{{project.*}}``.

    The request wins over the template's default. A default pointing at a
    deleted project resolves to None rather than 404ing — the FK is SET NULL,
    but a stale id in a request body should not break a preview either.
    """
    wanted = project_id if project_id is not None else template.default_project_id
    if wanted is None:
        return None
    if project_id is not None:
        return owned_project(project_id, db, user)
    project = db.get(Project, wanted)
    return project if project is not None and project.user_id == user.id else None


@router.get(
    "/builtins",
    response_model=list[BuiltinOut],
    summary="Placeholders every template can use",
    responses=errors(*AUTHENTICATED),
)
def list_builtins(
    user: User = Depends(get_current_user),
) -> list[BuiltinOut]:
    """Placeholders that always work, for the editor's insert menu.

    Served from the resolver's own table so the menu cannot offer a placeholder
    that renders empty forever.
    """
    return [BuiltinOut(name=name, description=desc) for name, desc in BUILTINS.items()]


@router.get(
    "",
    response_model=list[TemplateOut],
    summary="Your templates",
    # 404 only on the narrowed form, as on `GET /triggers`.
    responses=errors(*OWNED),
)
def list_templates(
    response: Response,
    project_id: int | None = Query(default=None),
    include_bodies: bool = Query(
        default=False,
        description="Carry each template's body_template, as this used to.",
    ),
    limit: int = Query(default=MAX_TEMPLATES_PER_USER, ge=1, le=MAX_TEMPLATES_PER_USER),
    offset: ListOffset = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TemplateOut]:
    """This user's templates, most recently edited first.

    Bodies are left out unless ``include_bodies=true`` asks for them, and the
    picker this feeds has never read one: it draws a name, a mode, a blank
    count and the placeholder chips. A body is up to 50,000 characters and an
    account may keep ``MAX_TEMPLATES_PER_USER`` of them, so the listing was a
    five-megabyte response — measured, not estimated — to render a hundred
    rows of one line each. The editor fetches the one template it is opening
    from ``GET /templates/{id}``, which is bounded by a single body rather than
    by how many the account has.

    Paged like every other listing, with the total in ``X-Total-Count``. The
    ceiling is the per-account cap rather than the 500 used elsewhere, since
    that cap is what bounds the collection.
    """
    query = (
        select(ContentTemplate)
        .where(ContentTemplate.user_id == user.id)
        .order_by(ContentTemplate.updated_at.desc(), ContentTemplate.id.desc())
    )
    if project_id is not None:
        owned_project(project_id, db, user)
        query = query.where(ContentTemplate.default_project_id == project_id)

    total = db.scalar(
        select(func.count()).select_from(
            query.with_only_columns(ContentTemplate.id).subquery()
        )
    )
    response.headers["X-Total-Count"] = str(total or 0)

    rows = db.scalars(query.limit(limit).offset(offset))
    return [_to_out(row, body=include_bodies) for row in rows]


@router.post(
    "",
    response_model=TemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a template",
    responses=errors(*OWNED, status.HTTP_409_CONFLICT),
)
def create_template(
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TemplateOut:
    """Save a new template.

    Two different 409s: one for the per-account ceiling, one for a name this
    account has already used. Names are unique per user because they are how
    the editor's template picker refers to them.
    """
    if payload.default_project_id is not None:
        owned_project(payload.default_project_id, db, user)

    count = (
        db.scalar(
            select(func.count(ContentTemplate.id)).where(
                ContentTemplate.user_id == user.id
            )
        )
        or 0
    )
    if count >= MAX_TEMPLATES_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"At most {MAX_TEMPLATES_PER_USER} templates.",
        )

    template = ContentTemplate(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        mode=payload.mode,
        content_type=payload.content_type,
        title_template=payload.title_template,
        body_template=payload.body_template,
        variables=[v.model_dump() for v in payload.variables],
        default_project_id=payload.default_project_id,
    )
    db.add(template)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have a template called {payload.name!r}.",
        ) from exc
    db.refresh(template)
    return _to_out(template)


@router.get(
    "/{template_id}",
    response_model=TemplateOut,
    summary="One template",
    responses=errors(*OWNED),
)
def get_template(
    template_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TemplateOut:
    """One template in full, including its variable definitions."""
    return _to_out(_owned(template_id, db, user))


@router.patch(
    "/{template_id}",
    response_model=TemplateOut,
    summary="Change a template",
    responses=errors(
        *OWNED,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
def update_template(
    template_id: RowId,
    payload: TemplateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TemplateOut:
    """Patch a template, re-validating the *result* rather than the patch.

    Deleting a variable and leaving its placeholder in the body is the mistake
    worth catching here, and it is invisible to a validator that only sees the
    fields that changed.
    """
    template = _owned(template_id, db, user)
    data = payload.model_dump(exclude_unset=True)

    if data.get("default_project_id") is not None:
        owned_project(data["default_project_id"], db, user)

    merged = {
        "name": template.name,
        "description": template.description,
        "mode": template.mode,
        "content_type": template.content_type,
        "title_template": template.title_template,
        "body_template": template.body_template,
        "variables": list(template.variables or []),
        "default_project_id": template.default_project_id,
        **data,
    }
    try:
        checked = TemplateCreate(**merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    template.name = checked.name
    template.description = checked.description
    template.mode = checked.mode
    template.content_type = checked.content_type
    template.title_template = checked.title_template
    template.body_template = checked.body_template
    template.variables = [v.model_dump() for v in checked.variables]
    template.default_project_id = checked.default_project_id

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have a template called {checked.name!r}.",
        ) from exc
    db.refresh(template)
    return _to_out(template)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a template",
    responses=errors(*OWNED),
)
def delete_template(
    template_id: RowId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Delete a template. The pieces it produced are untouched."""
    template = _owned(template_id, db, user)
    logger.info(
        "template %s (%s) deleted by user %s — had been used %s time(s)",
        template.id,
        template.name,
        user.id,
        template.use_count,
    )
    db.delete(template)
    db.commit()


@router.post(
    "/{template_id}/preview",
    response_model=RenderOut,
    summary="Render a template without saving",
    responses=errors(*OWNED),
)
def preview_template(
    template_id: RowId,
    payload: RenderRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RenderOut:
    """Render without saving, and without refusing on missing values.

    See the module docstring: a half-filled preview is the useful answer while
    the author is still typing.
    """
    template = _owned(template_id, db, user)
    project = _resolve_project(template, payload.project_id, db, user)
    rendered = template_service.render(template, payload.values, project=project)
    return RenderOut(
        title=rendered.title,
        body=rendered.body,
        missing=rendered.missing,
        filled=rendered.filled,
        is_complete=rendered.is_complete,
        over_limit=rendered.over_limit,
    )


@router.post(
    "/{template_id}/use",
    response_model=ContentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Turn a filled-in template into a draft",
    responses=errors(
        *OWNED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_ai_generate, key_func=account_key)
def use_template(
    template_id: RowId,
    payload: TemplateUseRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContentDetail:
    """Turn a filled-in template into a draft.

    A ``literal`` template never reaches a model; a ``prompt`` one hands its
    rendered text over as the brief.

    Rate-limited at the same per-account budget as ``/content/generate``,
    because in ``prompt`` mode this *is* ``/content/generate`` with the brief
    templated — the same call, against the same shared quota, reached by a
    different route. The budget is not conditioned on the mode: which mode a
    template is in cannot be known without reading the row, and the limit has
    to be decided before the request is worth serving. A ``literal`` template
    loses nothing real by it — sixty finished drafts an hour is far past what
    the deterministic path is for, and it writes a row either way.
    """
    from app.routers.content import _commit_content, _to_detail

    template = _owned(template_id, db, user)
    project = _resolve_project(template, payload.project_id, db, user)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Say which project this piece is for — the template has no "
            "default, or its default project is gone.",
        )

    rendered = template_service.render(template, payload.values, project=project)
    if rendered.missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Still needs a value for {', '.join(rendered.missing)}.",
        )
    if rendered.over_limit:
        # Refused rather than stored clipped, for the reason the preview is not:
        # this one writes a row, and a body that has been silently cut off no
        # longer matches the template that is meant to explain it. The amount of
        # slack is not obvious from the inputs either — each value is capped at
        # 5,000 characters, but a placeholder repeated ten thousand times in one
        # body template turns that into tens of megabytes — so the message says
        # which part overflowed and by how much rather than leaving the author
        # to work out which value to shorten.
        limits = {
            "title": template_service.TITLE_LIMIT,
            "body": template_service.BODY_LIMIT,
        }
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This template renders more than a piece can hold: "
                + "; ".join(
                    f"the {field} is over {limits[field]:,} characters"
                    for field in rendered.over_limit
                )
                + ". Shorten the values, or the template."
            ),
        )

    content_type = payload.content_type or template.content_type
    mode = (
        template.mode
        if isinstance(template.mode, TemplateMode)
        else TemplateMode(template.mode)
    )
    source = {
        "kind": "template",
        "user_id": user.id,
        "template_id": template.id,
        "template_name": template.name,
        "mode": mode.value,
    }

    if mode == TemplateMode.PROMPT:
        generated = content_generator.generate(
            project,
            content_type,
            instructions=rendered.body,
        )
        content = content_generator.content_from_generated(
            db,
            project_id=project.id,
            content_type=content_type,
            generated=generated,
            status=ContentStatus.DRAFT,
            source={**source, "fallback": generated.is_fallback},
        )
        # The author templated the headline for a reason: it is the part they
        # want consistent across a series. A rendered one therefore wins over
        # whatever the model came back with.
        if rendered.title:
            content.title = rendered.title
            content.slug = unique_content_slug(db, project.id, rendered.title)
    else:
        title = rendered.title or template.name
        keywords = seo.normalize_keywords(project.keywords or [])
        excerpt = template_service.excerpt_from(rendered.body)
        content = Content(
            project_id=project.id,
            content_type=content_type,
            status=ContentStatus.DRAFT,
            title=title,
            slug=unique_content_slug(db, project.id, title),
            body_markdown=rendered.body,
            excerpt=excerpt,
            meta_description=seo.build_meta_description(
                excerpt, fallback_body=rendered.body
            ),
            keywords=keywords,
            tags=[],
            focus_keyword=keywords[0] if keywords else "",
            # A literal template is the author's own writing, filled in
            # deterministically. There is nothing to be less than certain about.
            confidence=1.0,
            source=source,
        )

    template.use_count += 1
    _commit_content(db, content)
    db.commit()
    return _to_detail(content)
