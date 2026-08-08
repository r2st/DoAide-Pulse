"""Dev.to (Forem) adapter — the reference implementation.

Dev.to is the easiest of the six and the best-documented, so it is the adapter
to read first. One API key, Markdown in and Markdown out, a real stats endpoint,
and first-class canonical-URL support — which matters because Herald's whole
syndication model depends on the copies not competing with the original.

API: https://developers.forem.com/api/v1
"""
from __future__ import annotations

from typing import Any

from app.models.publication import Platform
from app.services.publishers import formatting
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    CredentialField,
    MetricsSnapshot,
    PublishError,
    PublishRequest,
    PublishResult,
)

_API = "https://dev.to/api"

#: Forem rejects the whole article if a tag has anything but alphanumerics, and
#: silently ignores a fifth.
_TAG_LIMIT = 4


class DevToAdapter(Adapter):
    platform = Platform.DEVTO
    display_name = "Dev.to"
    implemented = True
    utm_medium = "syndication"
    supports_metrics = True
    credential_fields = (
        CredentialField(
            key="api_key",
            label="API key",
            help_text="Dev.to → Settings → Extensions → DEV Community API Keys",
        ),
    )

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/vnd.forem.api-v1+json",
        }

    def verify(self, credentials: dict[str, Any]) -> str:
        (api_key,) = self._require(credentials, "api_key")
        resp = self._request("GET", f"{_API}/users/me", headers=self._headers(api_key))
        data = self._json(resp)
        username = data.get("username")
        if not username:
            raise CredentialError("Dev.to accepted the key but returned no account")
        return f"@{username}"

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        (api_key,) = self._require(credentials, "api_key")

        article: dict[str, Any] = {
            "title": request.title,
            "body_markdown": request.body_markdown,
            "published": not request.as_draft,
            "tags": formatting.normalize_tags(request.tags, limit=_TAG_LIMIT),
        }
        # Only send canonical_url when there is one: Forem 422s on an empty
        # string rather than treating it as absent.
        if request.canonical_url:
            article["canonical_url"] = request.canonical_url
        if request.meta_description:
            article["description"] = request.meta_description
        # Forem's name for the cover image. Same rule as canonical_url — an empty
        # string is a validation error, not "no image".
        if request.cover_image_url:
            article["main_image"] = request.cover_image_url

        resp = self._request(
            "POST",
            f"{_API}/articles",
            headers=self._headers(api_key),
            json_body={"article": article},
        )
        data = self._json(resp)

        article_id = data.get("id")
        if not article_id:
            raise PublishError(f"Dev.to returned no article id: {data}")

        return PublishResult(
            external_id=str(article_id),
            # A draft has no public URL yet; the edit link is the useful one.
            external_url=data.get("url") or f"https://dev.to/dashboard/{article_id}",
            extra={"slug": data.get("slug"), "published": data.get("published")},
        )

    def fetch_metrics(
        self, external_id: str, credentials: dict[str, Any]
    ) -> MetricsSnapshot:
        (api_key,) = self._require(credentials, "api_key")
        resp = self._request(
            "GET", f"{_API}/articles/{external_id}", headers=self._headers(api_key)
        )
        data = self._json(resp)
        # page_views_count is only present for the article's owner — exactly the
        # case we are in, but it stays optional in case that changes.
        return MetricsSnapshot(
            views=data.get("page_views_count"),
            reactions=data.get("public_reactions_count"),
            comments=data.get("comments_count"),
        )


__all__ = ["DevToAdapter"]
