"""WordPress adapter — scaffolded.

:meth:`build_payload` is real: it produces the ``/wp/v2/posts`` body, with the
Markdown already rendered to the HTML the block editor stores.

Of the four scaffolded platforms this is the closest to done — WordPress
application passwords are Basic auth, so there is no OAuth to build. It sits
behind Medium and Dev.to only because it needs a self-hosted site to test
against, and an adapter nobody has run once is not something to call finished.
Wiring it up is a ``_request`` with an ``Authorization: Basic`` header built
from ``username:application_password``.
"""
from __future__ import annotations

import base64
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


class WordPressAdapter(Adapter):
    platform = Platform.WORDPRESS
    display_name = "WordPress"
    implemented = False
    utm_medium = "syndication"
    supports_metrics = False
    caveat = (
        "Uses application passwords (Basic auth) — no OAuth needed. The publish "
        "call is written but untested against a live site."
    )
    credential_fields = (
        CredentialField(
            key="site_url",
            label="Site URL",
            help_text="e.g. https://blog.example.com — the REST API root is derived from it.",
            secret=False,
        ),
        CredentialField(key="username", label="Username", secret=False),
        CredentialField(
            key="application_password",
            label="Application password",
            help_text="Users → Profile → Application Passwords. Not your login password.",
        ),
    )

    def api_root(self, site_url: str) -> str:
        return f"{site_url.rstrip('/')}/wp-json/wp/v2"

    def auth_header(self, username: str, application_password: str) -> str:
        """Basic auth header. Application passwords arrive space-separated."""
        token = f"{username}:{application_password.replace(' ', '')}"
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"

    def build_payload(self, request: PublishRequest) -> dict[str, Any]:
        """The ``POST /wp/v2/posts`` body.

        Tags are sent as names via ``tags_input``; the taxonomy endpoint wants
        term *ids*, and resolving names to ids is the one genuinely fiddly part
        of this adapter.
        """
        payload: dict[str, Any] = {
            "title": request.title,
            # The cover is inlined as the first block rather than set as
            # `featured_media`: that field takes a media *id*, so using it means
            # uploading the bytes to /wp/v2/media first. Herald holds a URL, not
            # the image, and the first image in the content is what most themes
            # fall back to for the archive thumbnail anyway.
            "content": formatting.lead_image_html(
                request.cover_image_url or "", alt=request.title
            )
            + formatting.to_html(request.body_markdown),
            "status": "draft" if request.as_draft else "publish",
        }
        if request.excerpt:
            payload["excerpt"] = request.excerpt
        tags = formatting.normalize_tags(request.tags, limit=8, allow_spaces=True)
        if tags:
            payload["tags_input"] = tags
        return payload

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        raise NotImplementedAdapter(
            "The WordPress adapter is not finished — its publish call has not been "
            "tested against a live site. Content queued for WordPress will stay "
            "pending."
        )


__all__ = ["WordPressAdapter"]
