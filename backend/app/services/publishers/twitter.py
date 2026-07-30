"""Twitter/X adapter — scaffolded.

:meth:`build_thread` is real and tested: it turns a blog post into a thread that
fits inside the character limit, with the link on the first tweet and hashtags
on the last. What is missing is the API call.

Two reasons it is not in the first pass. The v2 ``POST /2/tweets`` endpoint needs
OAuth 1.0a request signing or a three-legged OAuth 2.0 token — a pasted bearer
token only reads. And write access is a paid tier, so a working adapter would
fail for anyone on the free plan anyway, which is a worse experience than an
honest "not connected".
"""
from __future__ import annotations

from typing import Any

from app.models.publication import Platform
from app.services.publishers import formatting
from app.services.publishers.base import (
    Adapter,
    CredentialField,
    NotImplementedAdapter,
    PublishRequest,
    PublishResult,
)

#: Longer than this and it stops being a thread and starts being a blog post
#: nobody scrolls.
_MAX_TWEETS = 5


class TwitterAdapter(Adapter):
    platform = Platform.TWITTER
    display_name = "Twitter / X"
    implemented = False
    utm_medium = "social"
    supports_metrics = False
    caveat = (
        "Needs OAuth 1.0a signing (or 3-legged OAuth 2.0) and a paid API tier "
        "for write access."
    )
    credential_fields = (
        CredentialField(key="api_key", label="API key"),
        CredentialField(key="api_secret", label="API key secret"),
        CredentialField(key="access_token", label="Access token"),
        CredentialField(key="access_token_secret", label="Access token secret"),
    )

    def build_thread(self, request: PublishRequest) -> list[str]:
        """Split a post into a thread of tweets.

        The hook is the excerpt plus the link, because a thread's first tweet is
        the only one most people see. Body paragraphs follow, each numbered so a
        reader landing mid-thread knows there is more, and the hashtags go last
        where they cost no space in the hook.
        """
        link = request.link
        hook = formatting.truncate_for_tweet(
            request.excerpt or request.title, url=link
        )
        thread = [hook]

        paragraphs = [
            para.strip()
            for para in formatting.to_plain_text(request.body_markdown).split("\n\n")
            if len(para.strip()) > 40
        ]

        # Reserve the numbering suffix before truncating, or " 2/4" pushes the
        # tweet one character over.
        body_slots = _MAX_TWEETS - 1
        for index, para in enumerate(paragraphs[:body_slots], start=2):
            total = min(len(paragraphs), body_slots) + 1
            suffix = f" {index}/{total}"
            thread.append(
                formatting.truncate_for_tweet(para[: formatting.TWEET_LIMIT * 2])[
                    : formatting.TWEET_LIMIT - len(suffix)
                ].rstrip()
                + suffix
            )

        tags = formatting.hashtagify(request.tags, limit=3)
        if tags and len(thread) < _MAX_TWEETS:
            thread.append(tags)
        return thread

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        raise NotImplementedAdapter(
            "The Twitter/X adapter is not finished — it needs OAuth 1.0a signing "
            "and a paid API tier. Content queued for Twitter will stay pending."
        )


__all__ = ["TwitterAdapter"]
