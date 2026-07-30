"""Medium adapter.

Implemented against Medium's documented Publishing API (integration tokens,
``POST /v1/users/{id}/posts``).

**Read this before connecting Medium.** Medium stopped issuing new integration
tokens in 2023 and the API is no longer actively maintained. Tokens created
before then still work, which is why this adapter exists and is wired up — but a
new account cannot get one. The connection flow surfaces :attr:`caveat` so
nobody discovers that after writing a post. Nothing else in Herald depends on
Medium working; a failed publication is per-platform (see the ``publications``
table), so the same piece still goes out everywhere else.

Two Medium-specific behaviours worth knowing:

* Posts are created via ``contentFormat: "html"`` here rather than "markdown".
  Medium's Markdown handling drops fenced code-block language hints, which is
  most of the value of a code block in a technical post.
* ``canonicalUrl`` is honoured, so syndicating to Medium after publishing
  elsewhere does not split the search ranking.

API: https://github.com/Medium/medium-api-docs
"""
from __future__ import annotations

from typing import Any

from app.models.publication import Platform
from app.services.publishers import formatting
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    CredentialField,
    PublishError,
    PublishRequest,
    PublishResult,
)

_API = "https://api.medium.com/v1"

#: Medium accepts at most five tags and drops the rest silently.
_TAG_LIMIT = 5


class MediumAdapter(Adapter):
    platform = Platform.MEDIUM
    display_name = "Medium"
    implemented = True
    # Medium's API exposes no read/clap counts for a post. The stats live behind
    # the web UI only, so the metrics poller skips this platform.
    supports_metrics = False
    caveat = (
        "Medium stopped issuing new integration tokens in 2023. This works with "
        "a token created before then; new accounts cannot obtain one."
    )
    credential_fields = (
        CredentialField(
            key="integration_token",
            label="Integration token",
            help_text="Medium → Settings → Security and apps → Integration tokens",
        ),
        CredentialField(
            key="publication_id",
            label="Publication ID",
            help_text="Optional. Leave blank to post to your own profile.",
            secret=False,
            required=False,
        ),
    )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Charset": "utf-8",
        }

    def _me(self, token: str) -> dict[str, Any]:
        resp = self._request("GET", f"{_API}/me", headers=self._headers(token))
        data = (resp.json() or {}).get("data") or {}
        if not data.get("id"):
            raise CredentialError("Medium accepted the token but returned no user")
        return data

    def verify(self, credentials: dict[str, Any]) -> str:
        (token,) = self._require(credentials, "integration_token")
        data = self._me(token)
        return f"@{data.get('username')}" if data.get("username") else data["name"]

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        (token,) = self._require(credentials, "integration_token")
        publication_id = str(credentials.get("publication_id") or "").strip()

        payload: dict[str, Any] = {
            "title": request.title,
            "contentFormat": "html",
            # Medium does not render a title from the payload — it has to be in
            # the body as an H1, or the post opens with no headline. The cover
            # goes *above* it: Medium has no cover parameter and takes the first
            # image in the body as the post's preview image.
            "content": formatting.lead_image_html(
                request.cover_image_url or "", alt=request.title
            )
            + f"<h1>{_escape(request.title)}</h1>"
            + formatting.to_html(request.body_markdown),
            "tags": formatting.normalize_tags(
                request.tags, limit=_TAG_LIMIT, allow_spaces=True
            ),
            "publishStatus": "draft" if request.as_draft else "public",
        }
        if request.canonical_url:
            payload["canonicalUrl"] = request.canonical_url

        if publication_id:
            url = f"{_API}/publications/{publication_id}/posts"
        else:
            url = f"{_API}/users/{self._me(token)['id']}/posts"

        resp = self._request("POST", url, headers=self._headers(token), json_body=payload)
        data = (resp.json() or {}).get("data") or {}

        post_id = data.get("id")
        if not post_id:
            raise PublishError(f"Medium returned no post id: {data}")

        return PublishResult(
            external_id=str(post_id),
            external_url=data.get("url", ""),
            extra={"publish_status": data.get("publishStatus")},
        )


def _escape(text: str) -> str:
    """Minimal HTML escaping for the title we inject as an H1."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["MediumAdapter"]
