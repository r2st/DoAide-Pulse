"""LinkedIn adapter — scaffolded.

:meth:`build_payload` is real: it produces the ``ugcPosts`` body LinkedIn's
Share API expects, with the post already reduced to plain text and trimmed to
the platform's limits. What is missing is OAuth.

LinkedIn is the only one of the six that cannot work from a pasted API key. The
Share API needs a three-legged OAuth token with the ``w_member_social`` scope,
which means a registered LinkedIn app, a redirect URI, and a token-refresh
cycle — a whole auth flow rather than an adapter. That is why it is scaffolded:
the content side is done, the identity side is a separate piece of work.
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


class LinkedInAdapter(Adapter):
    platform = Platform.LINKEDIN
    display_name = "LinkedIn"
    implemented = False
    utm_medium = "social"
    hosts_canonical = False
    supports_metrics = False
    caveat = (
        "Needs a registered LinkedIn app and a three-legged OAuth token "
        "(w_member_social). Not connectable with a pasted key."
    )
    credential_fields = (
        CredentialField(
            key="access_token",
            label="OAuth access token",
            help_text="Requires the w_member_social scope.",
        ),
        CredentialField(
            key="author_urn",
            label="Author URN",
            help_text="e.g. urn:li:person:XXXX — the profile that owns the post.",
            secret=False,
        ),
    )

    def build_commentary(self, request: PublishRequest) -> str:
        """The post text: excerpt, link, hashtags — trimmed to fit.

        LinkedIn renders no markup at all, so the body is flattened to plain
        text. The excerpt leads because the first two lines are all that show
        above the "…see more" fold.

        The title is the last fallback, and it is the one this was missing.
        Mastodon and Bluesky both compose from ``excerpt or title``; here the
        second term was the flattened body, which has a case the other two do
        not — it can come back **empty**. A piece whose body is images and
        nothing else (``![diagram](…)`` twice over, which is what a changelog
        built from screenshots looks like) flattens to the empty string, because
        an ``<img>`` has no text in it. With no excerpt either, the post that
        went out was two blank lines and a bare link: no title, no words, and
        nothing saying what it pointed at. A piece always has a title —
        ``ContentCreate`` requires one and ``_assemble`` substitutes
        ``"{project}: {label}"`` when the model does not supply it — so there is
        always something better than nothing to lead with.
        """
        lead = (
            request.excerpt
            or formatting.to_plain_text(request.body_markdown)
            or request.title
        )
        tags = formatting.hashtagify(request.tags, limit=3)
        if tags:
            lead = f"{lead}\n\n{tags}"
        link = request.link
        return formatting.truncate_for_linkedin(lead, url=link)

    def build_payload(self, request: PublishRequest, author_urn: str) -> dict[str, Any]:
        """The ``/v2/ugcPosts`` request body."""
        return {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": self.build_commentary(request)},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Always raises. The adapter needs an OAuth flow before it can post."""
        raise NotImplementedAdapter(
            "The LinkedIn adapter is not finished — it needs an OAuth flow before "
            "it can post. Anything queued for LinkedIn fails immediately rather "
            "than waiting for an adapter that is not coming."
        )


__all__ = ["LinkedInAdapter"]
