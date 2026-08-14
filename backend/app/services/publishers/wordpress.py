"""WordPress adapter.

Self-hosted WordPress over the REST API, authenticated with an *application
password* — Basic auth, issued per application, revocable on its own, and not
the account password. No OAuth, which is what makes the largest publishing
platform in the world one of the cheapest destinations to support.

Two site-shaped things this has to tolerate, because they vary per install
rather than per account:

* **The REST root moves.** ``/wp-json`` is the default, but a site on plain
  permalinks serves it at ``/?rest_route=``. Only the default is supported; a
  site on the other gets a clear 404 rather than silent nonsense.
* **Basic auth is sometimes stripped.** Apache in CGI mode drops the
  ``Authorization`` header before PHP sees it, and the symptom is a 401 with
  perfectly good credentials. :attr:`caveat` says so, because the generic
  "reconnect the account" advice is wrong for that one cause.

API: https://developer.wordpress.org/rest-api/reference/posts/
"""
from __future__ import annotations

import base64
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


class WordPressAdapter(Adapter):
    platform = Platform.WORDPRESS
    display_name = "WordPress"
    implemented = True
    utm_medium = "syndication"
    # The REST API reports no view counts — those live in Jetpack Stats or
    # whatever analytics the site runs, neither of which is core.
    supports_metrics = False
    # `site_url` is typed into the settings form, so every request this adapter
    # makes is one an account holder chose the address of. See `Adapter._send`.
    user_supplied_host = True
    caveat = (
        "Self-hosted WordPress with the REST API at /wp-json. Uses an "
        "application password (Users → Profile → Application Passwords), not "
        "your login password. If a correct password gets a 401, the host is "
        "stripping the Authorization header."
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
        """The REST base for a site. Trailing slashes on *site_url* are tolerated."""
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
        # SEO meta — sent via the `meta` field so Yoast SEO (or compatible
        # plugins) can pick them up.  Sites without Yoast ignore unknown meta
        # keys, so this is safe either way.
        meta: dict[str, str] = {}
        if request.meta_description:
            meta["_yoast_wpseo_metadesc"] = request.meta_description
        focus = request.focus_keyword or ""
        if focus:
            meta["_yoast_wpseo_focuskw"] = focus
        if request.canonical_url:
            meta["_yoast_wpseo_canonical"] = request.canonical_url
        if meta:
            payload["meta"] = meta
        return payload

    def _headers(self, username: str, application_password: str) -> dict[str, str]:
        return {
            "Authorization": self.auth_header(username, application_password),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def verify(self, credentials: dict[str, Any]) -> str:
        """Return the display name behind the application password.

        A 200 with no user in it means the REST API is not reachable at
        ``/wp-json`` — commonly a security plugin — which is worth saying plainly
        rather than reporting as a bad password.
        """
        site_url, username, password = self._require(
            credentials, "site_url", "username", "application_password"
        )
        resp = self._request(
            "GET",
            f"{self.api_root(site_url)}/users/me",
            headers=self._headers(username, password),
        )
        data = self._json(resp) or {}
        if not data.get("id"):
            raise CredentialError(
                "WordPress accepted the request but returned no user — check "
                "that the REST API is reachable at /wp-json."
            )
        return data.get("name") or data.get("slug") or username

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Create a post. Tags are sent as names; see :meth:`build_payload`."""
        site_url, username, password = self._require(
            credentials, "site_url", "username", "application_password"
        )

        resp = self._request(
            "POST",
            f"{self.api_root(site_url)}/posts",
            headers=self._headers(username, password),
            json_body=self.build_payload(request),
        )
        data = self._json(resp) or {}

        post_id = data.get("id")
        if not post_id:
            raise PublishError(f"WordPress returned no post id: {data}")

        # `link` is the permalink; a draft has none, so fall back to the editor.
        url = data.get("link") or (
            f"{site_url.rstrip('/')}/wp-admin/post.php?post={post_id}&action=edit"
        )
        return PublishResult(
            external_id=str(post_id),
            external_url=url,
            extra={"status": data.get("status"), "slug": data.get("slug")},
        )


__all__ = ["WordPressAdapter"]
