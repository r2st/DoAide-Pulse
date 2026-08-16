"""The shapes Herald puts in a webhook body.

Written by hand rather than dumped from the response schemas, and kept in their
own module rather than beside whichever service happens to emit them. Both
choices are the same choice: a webhook payload is a published contract with code
Herald cannot see, so it should change when somebody decides it changes — not
when a router's response model gains a field, and not differently in two places
because two services grew their own copy.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.content import Content
    from app.models.publication import Publication


def content_payload(content: Content) -> dict:
    """One piece of content, as a receiver sees it."""
    project = content.project
    return {
        "id": content.id,
        "title": content.title,
        "slug": content.slug,
        "content_type": content.content_type.value,
        "status": content.status.value,
        "canonical_url": content.canonical_url,
        "excerpt": content.excerpt,
        "word_count": content.word_count,
        "published_at": (
            content.published_at.isoformat() if content.published_at else None
        ),
        "project": (
            {"id": project.id, "name": project.name, "slug": project.slug}
            if project
            else None
        ),
    }


def publication_payload(publication: Publication) -> dict:
    """One piece's outcome on one platform."""
    return {
        "id": publication.id,
        "platform": publication.platform.value,
        "status": publication.status.value,
        "external_url": publication.external_url,
        "attempts": publication.attempts,
        "error": publication.error,
    }


def engagement_payload(
    content: Content, *, threshold: int, engagement: int, views: int, platforms: list[str]
) -> dict:
    """A piece that passed its project's engagement threshold.

    Carries the threshold as well as the number that crossed it. A receiver
    reading ``engagement: 214`` alone cannot tell whether that is remarkable;
    with ``threshold: 200`` beside it, the message writes itself — and a
    receiver that has several projects pointed at one endpoint can tell which
    bar was cleared without keeping its own copy of the settings.

    ``views`` is included but is not what the threshold is measured against —
    see :attr:`app.models.project.Project.engagement_threshold` for why. It is
    here because it is the number a human wants in the same sentence.
    """
    return {
        "content": content_payload(content),
        "threshold": threshold,
        "engagement": engagement,
        "views": views,
        "platforms": platforms,
    }


__all__ = ["content_payload", "engagement_payload", "publication_payload"]
