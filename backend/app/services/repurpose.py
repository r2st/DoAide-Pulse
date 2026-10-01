"""Turn a long-form piece into ready-to-paste social snippets.

Pulse already builds a Twitter thread and a LinkedIn post at publish time
(``TwitterAdapter.build_thread`` / ``LinkedInAdapter.build_commentary``) — but
those are mechanical: the thread is the body cut into paragraph-sized chunks,
and the LinkedIn post is just the excerpt. Neither adapter can actually publish
yet (both need an OAuth flow Pulse doesn't have), so today that text is
invisible to a user who would happily copy-paste it by hand.

This module asks a model to do better than paragraph-splitting — one idea per
tweet, a LinkedIn post that reads like it was written for LinkedIn rather than
clipped from a blog — and falls back to the existing mechanical builders when
every provider is down. Same shape as ``app.services.content_generator``: a
JSON envelope in, a dataclass out, never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.content import Content
from app.models.project import Project
from app.services import ai
from app.services.publishers import formatting
from app.services.publishers.base import PublishRequest
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.twitter import TwitterAdapter

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write short-form social media copy that repurposes a long-form "
    "article. You never invent facts, statistics or quotes that are not in "
    "the source. You always reply with a single JSON object and nothing else."
)

#: Free reasoning models burn a near-fixed scratchpad allowance regardless of
#: how short the requested output is — see the constant of the same name in
#: app.services.content_generator, where the number was measured.
_REASONING_ALLOWANCE_TOKENS = 5000
_OUTPUT_TOKENS = 1200

#: Below this many words, "repurposing" is just repeating — the source has too
#: little to make a thread and a post that say different things.
_MIN_SOURCE_WORDS = 60


@dataclass
class RepurposedContent:
    """Social snippets derived from one piece of content."""

    twitter_thread: list[str] = field(default_factory=list)
    linkedin_post: str = ""
    provider: str | None = None
    model: str | None = None
    #: True when every provider failed and this is the mechanical fallback —
    #: the same builders the (unfinished) Twitter/LinkedIn adapters use.
    is_fallback: bool = False


def _publish_request(content: Content, project: Project) -> PublishRequest:
    """A flat request object, built the same way the publish path does.

    Shared with the fallback path so the mechanical builders see exactly the
    fields they would at real publish time.
    """
    return PublishRequest(
        title=content.title,
        body_markdown=content.body_markdown,
        excerpt=content.excerpt,
        meta_description=content.meta_description,
        tags=list(content.tags or []),
        keywords=list(content.keywords or []),
        focus_keyword=content.focus_keyword or "",
        slug=content.slug,
        canonical_url=content.canonical_url,
        project_url=project.live_url,
        project_name=project.name,
    )


def _fallback(request: PublishRequest) -> RepurposedContent:
    """The mechanical builders — no model involved, so this never fails."""
    return RepurposedContent(
        twitter_thread=TwitterAdapter().build_thread(request),
        linkedin_post=LinkedInAdapter().build_commentary(request),
        is_fallback=True,
    )


def _build_prompt(content: Content, plain_body: str) -> list[dict[str, str]]:
    facts = [f"Title: {content.title}"]
    if content.excerpt:
        facts.append(f"Excerpt: {content.excerpt}")
    facts.append("Body:\n" + plain_body)
    if content.tags:
        facts.append("Tags: " + ", ".join(content.tags))

    user_prompt = f"""Repurpose this article into social media posts.

{chr(10).join(facts)}

Rules:
- Twitter thread: 3-5 tweets, each a self-contained idea (not a paragraph cut
  in half). Leave room on the first tweet for a link to be appended after it.
  No hashtags except optionally on the last tweet.
- LinkedIn post: one post, professional but not stiff, short paragraphs, no
  markdown syntax, ending with up to 3 hashtags.
- Only use facts, numbers and claims that appear in the article above.

Reply with exactly this JSON object and nothing else:
{{"twitter_thread": ["tweet 1", "tweet 2", "..."], "linkedin_post": "the full post text"}}"""

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def generate(content: Content, project: Project) -> RepurposedContent:
    """Draft social snippets for one piece. Never raises — falls back to the
    mechanical builders when the provider chain is unusable or the source is
    too thin to say anything a mechanical clip wouldn't already say."""
    request = _publish_request(content, project)
    link = request.link

    plain_body = formatting.to_plain_text(content.body_markdown)
    if len(plain_body.split()) < _MIN_SOURCE_WORDS:
        return _fallback(request)

    try:
        payload, completion = ai.json_completion(
            _build_prompt(content, plain_body[:4000]),
            temperature=0.6,
            max_tokens=_OUTPUT_TOKENS + _REASONING_ALLOWANCE_TOKENS,
            purpose="repurpose",
        )
    except ai.AIError as exc:
        logger.info("repurposing content %s fell back to mechanical builders: %s", content.id, exc)
        return _fallback(request)

    raw_tweets = [t for t in ai.as_str_list(payload.get("twitter_thread"), limit=6) if t]
    thread = [
        formatting.truncate_for_tweet(tweet, url=link if index == 0 else None)
        for index, tweet in enumerate(raw_tweets)
    ]
    linkedin_post = ai.as_str(payload.get("linkedin_post"))
    if linkedin_post:
        linkedin_post = formatting.truncate_for_linkedin(linkedin_post, url=link)

    unusable = (
        not thread
        or not linkedin_post
        or any(ai.looks_like_reasoning(t) for t in thread)
        or ai.looks_like_reasoning(linkedin_post)
    )
    if unusable:
        logger.warning(
            "repurposing content %s returned unusable output — using mechanical builders",
            content.id,
        )
        return _fallback(request)

    return RepurposedContent(
        twitter_thread=thread,
        linkedin_post=linkedin_post,
        provider=completion.provider,
        model=completion.model,
        is_fallback=False,
    )


__all__ = ["RepurposedContent", "generate"]
