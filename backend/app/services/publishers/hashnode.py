"""Hashnode adapter — scaffolded.

The formatting half is real and tested: :meth:`build_payload` produces the exact
GraphQL variables Hashnode's ``publishPost`` mutation expects. What is missing is
the network call and the publication-id lookup, so :meth:`publish` refuses
rather than pretending.

Hashnode is GraphQL-only (https://gql.hashnode.com), which is why it is not in
the first pass: every other adapter is a REST POST, and the mutation needs a
``publicationId`` the user has to find in their blog dashboard. Wiring it up is
a ``_request`` call to the GraphQL endpoint plus a ``me.publications`` query for
:meth:`verify`.
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

_TAG_LIMIT = 5


class HashnodeAdapter(Adapter):
    platform = Platform.HASHNODE
    display_name = "Hashnode"
    implemented = False
    utm_medium = "syndication"
    supports_metrics = False
    caveat = "Formatting is implemented; the GraphQL publish call is not wired up yet."
    credential_fields = (
        CredentialField(
            key="api_key",
            label="Personal access token",
            help_text="Hashnode → Account settings → Developer → Personal Access Tokens",
        ),
        CredentialField(
            key="publication_id",
            label="Publication ID",
            help_text="Found in your blog's dashboard URL.",
            secret=False,
        ),
    )

    def build_payload(self, request: PublishRequest, publication_id: str) -> dict[str, Any]:
        """GraphQL variables for the ``publishPost`` mutation.

        Hashnode takes Markdown directly, so the body passes through untouched —
        the work here is the metadata shape and the tag vocabulary.
        """
        payload: dict[str, Any] = {
            "publicationId": publication_id,
            "title": request.title,
            "contentMarkdown": request.body_markdown,
            "tags": [
                {"slug": tag, "name": tag}
                for tag in formatting.normalize_tags(request.tags, limit=_TAG_LIMIT)
            ],
        }
        if request.meta_description:
            payload["metaTags"] = {
                "title": request.title,
                "description": request.meta_description,
            }
        if request.canonical_url:
            payload["originalArticleURL"] = request.canonical_url
        if request.cover_image_url:
            payload["coverImageOptions"] = {"coverImageURL": request.cover_image_url}
        return payload

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        raise NotImplementedAdapter(
            "The Hashnode adapter is not finished — its GraphQL publish call is "
            "still to be written. Content queued for Hashnode will stay pending."
        )


__all__ = ["HashnodeAdapter"]
