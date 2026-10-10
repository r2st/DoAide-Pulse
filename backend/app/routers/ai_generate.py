"""AI-powered field generation using the existing LLM provider chain."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import AUTHENTICATED, errors
from app.schemas.limits import FieldName
from app.services import ai

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


class GenerateFieldsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=500)
    body_markdown: str = Field(default="", max_length=200_000)
    excerpt: str = Field(default="", max_length=1000)
    fields: list[FieldName] = Field(
        min_length=1,
        max_length=10,
        description="Which fields to generate: meta_description, keywords, tags, excerpt, cover_image_prompt",
    )


class GenerateFieldsResponse(BaseModel):
    meta_description: str | None = None
    keywords: list[str] | None = None
    tags: list[str] | None = None
    excerpt: str | None = None
    cover_image_prompt: str | None = None
    provider: str | None = None
    model: str | None = None


def _build_prompt(req: GenerateFieldsRequest) -> str:
    parts = [
        "You are a content marketing expert. Given the following article, "
        "generate the requested fields as a JSON object.\n\n"
    ]
    if req.title:
        parts.append(f"TITLE: {req.title}\n\n")
    if req.body_markdown:
        body_preview = req.body_markdown[:3000]
        parts.append(f"BODY (first 3000 chars):\n{body_preview}\n\n")
    if req.excerpt:
        parts.append(f"EXISTING EXCERPT: {req.excerpt}\n\n")

    parts.append("Generate the following fields as a JSON object:\n")
    field_instructions = {
        "meta_description": "- meta_description: A compelling SEO meta description, 120-155 characters. Summarize the article's value proposition.",
        "keywords": "- keywords: A list of 3-8 SEO keywords/phrases relevant to the article content.",
        "tags": "- tags: A list of 3-5 platform tags suitable for Dev.to, Medium, and Hashnode. Use lowercase, no spaces (use hyphens).",
        "excerpt": "- excerpt: A 1-2 sentence summary that hooks the reader. Different from meta_description — this is for social sharing.",
        "cover_image_prompt": "- cover_image_prompt: A detailed prompt for generating a cover image with AI image tools. Describe the visual concept, style, colors, and mood.",
    }
    for field in req.fields:
        if field in field_instructions:
            parts.append(field_instructions[field] + "\n")

    parts.append("\nRespond with ONLY a JSON object containing these fields. No markdown, no explanation.")
    return "".join(parts)


@router.post(
    "/generate-fields",
    response_model=GenerateFieldsResponse,
    summary="AI-generate SEO and content fields",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_429_TOO_MANY_REQUESTS,
        *AUTHENTICATED,
    ),
)
@limiter.limit(settings.rate_limit_ai_assist, key_func=account_key)
def generate_fields(
    request: Request,
    response: Response,
    req: GenerateFieldsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GenerateFieldsResponse:
    """Use the LLM provider chain to generate SEO and content metadata."""
    if not req.title and not req.body_markdown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of title or body_markdown is required",
        )

    valid_fields = {"meta_description", "keywords", "tags", "excerpt", "cover_image_prompt"}
    invalid = set(req.fields) - valid_fields
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown fields: {', '.join(sorted(invalid))}. "
            f"Valid: {', '.join(sorted(valid_fields))}",
        )

    prompt = _build_prompt(req)

    try:
        result, completion = ai.json_completion(
            [{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=800,
            purpose="field_generation",
        )
    except ai.AIError as exc:
        logger.warning("AI field generation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation is temporarily unavailable. Try again in a moment.",
        ) from exc

    fields: dict = {"provider": completion.provider, "model": completion.model}

    if "meta_description" in req.fields:
        fields["meta_description"] = ai.as_str(result.get("meta_description"))[:155]
    if "keywords" in req.fields:
        fields["keywords"] = ai.as_str_list(result.get("keywords"), limit=8)
    if "tags" in req.fields:
        fields["tags"] = ai.as_str_list(result.get("tags"), limit=5)
    if "excerpt" in req.fields:
        fields["excerpt"] = ai.as_str(result.get("excerpt"))[:1000]
    if "cover_image_prompt" in req.fields:
        fields["cover_image_prompt"] = ai.as_str(result.get("cover_image_prompt"))[:500]

    return GenerateFieldsResponse(**fields)
