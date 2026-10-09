"""Public-facing viral features: shareable articles, subscriber collection,
and the free content idea generator."""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.content import Content, ContentStatus
from app.models.subscriber import Subscriber
from app.ratelimit import limiter
from app.schemas.errors import ErrorOut, errors
from app.schemas.viral import (
    ContentIdeaItem,
    ContentIdeasIn,
    ContentIdeasOut,
    PublicArticleOut,
    SubscribeIn,
    SubscribeOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["viral"])


@router.get(
    "/articles/{slug}",
    response_model=PublicArticleOut,
    summary="A published article, publicly accessible",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorOut,
            "description": "No published article with this slug.",
        },
        **errors(status.HTTP_429_TOO_MANY_REQUESTS),
    },
)
@limiter.limit(settings.rate_limit_public_read)
def get_public_article(
    request: Request,
    response: Response,
    slug: str = Path(max_length=320),
    db: Session = Depends(get_db),
) -> PublicArticleOut:
    """Fetch a published article by slug. Unauthenticated."""
    content = db.scalar(
        select(Content).where(
            Content.slug == slug,
            Content.status == ContentStatus.PUBLISHED,
        )
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Article not found.",
        )
    return PublicArticleOut(
        title=content.title,
        slug=content.slug,
        body_markdown=content.body_markdown,
        excerpt=content.excerpt,
        cover_image_url=content.cover_image_url,
        meta_description=content.meta_description,
        tags=content.tags,
        word_count=content.word_count,
        read_minutes=content.read_minutes,
        project_name=content.project.name if content.project else None,
        published_at=content.published_at.isoformat() if content.published_at else None,
    )


@router.post(
    "/subscribers",
    response_model=SubscribeOut,
    summary="Collect a newsletter subscriber email",
    responses=errors(status.HTTP_429_TOO_MANY_REQUESTS),
)
@limiter.limit("10/minute;100/hour")
def subscribe(
    request: Request,
    response: Response,
    payload: SubscribeIn,
    db: Session = Depends(get_db),
) -> SubscribeOut:
    """Collect an email from the embed widget or public pages. Unauthenticated."""
    sub = Subscriber(email=payload.email.lower(), source=payload.source)
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return SubscribeOut(ok=True)


@router.post(
    "/tools/content-ideas",
    response_model=ContentIdeasOut,
    summary="AI-powered content idea generator (free, no login)",
    responses=errors(
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
@limiter.limit("5/minute;30/hour")
def generate_content_ideas(
    request: Request,
    response: Response,
    payload: ContentIdeasIn,
) -> ContentIdeasOut:
    """Generate content ideas for a niche using Gemini. No auth required."""
    api_key = settings.gemini_api_key
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Content idea generation is temporarily unavailable.",
        )

    prompt = (
        f"You are an expert content strategist. Generate {payload.count} unique, "
        f"engaging content ideas for the niche: \"{payload.niche}\".\n\n"
        "For each idea, provide:\n"
        "- title: A compelling headline\n"
        "- hook: A one-sentence hook explaining why readers would care\n"
        "- content_type: One of: tutorial, how_to, comparison, announcement, "
        "feature_spotlight, product_spotlight\n\n"
        "Respond with ONLY a JSON array of objects. No markdown, no explanation.\n"
        "Example: [{\"title\": \"...\", \"hook\": \"...\", \"content_type\": \"tutorial\"}]"
    )

    try:
        resp = httpx.post(
            f"{settings.gemini_base_url}chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gemini-2.0-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "max_tokens": 2000,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Gemini API call failed for content ideas")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Content idea generation is temporarily unavailable.",
        )

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        ideas_raw = json.loads(text)
        ideas = [
            ContentIdeaItem(
                title=item.get("title", "Untitled"),
                hook=item.get("hook", ""),
                content_type=item.get("content_type", "tutorial"),
            )
            for item in ideas_raw[:payload.count]
        ]
    except (KeyError, json.JSONDecodeError, IndexError):
        logger.exception("Failed to parse Gemini response for content ideas")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not parse AI response. Please try again.",
        )

    return ContentIdeasOut(ideas=ideas, niche=payload.niche)
