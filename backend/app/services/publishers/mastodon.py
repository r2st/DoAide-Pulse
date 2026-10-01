"""Mastodon adapter.

The cheapest working destination in the product. Mastodon's write API is a form
POST with a bearer token — no OAuth dance, no paid tier, no approval queue — and
the audience on the technical instances is exactly Pulse's. Getting a token is
four clicks: Preferences → Development → New application, tick ``write:statuses``
(and ``read:accounts``, which is what :meth:`verify` uses).

There is no instance to hard-code, because there is no such thing as *the*
Mastodon: the instance URL is part of the credentials, and everything else in
this adapter is the same wherever it points.

Two behaviours worth knowing:

* **No drafts.** Mastodon has scheduled statuses and nothing else, so
  ``as_draft`` raises :class:`UnsupportedOption` rather than quietly publishing
  live. Somebody who ticked that box did it to avoid exactly that.
* **Idempotency is real here.** The ``Idempotency-Key`` header makes a repeat of
  the same request return the original status instead of posting a second copy,
  which is what a retry after a timeout would otherwise do.

API: https://docs.joinmastodon.org/methods/statuses/
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.models.publication import Platform
from app.services.publishers import formatting
from app.services.publishers.base import (
    PREFLIGHT_ERROR,
    PREFLIGHT_WARNING,
    Adapter,
    CredentialError,
    CredentialField,
    MetricsSnapshot,
    PreflightFinding,
    PublishError,
    PublishRequest,
    PublishResult,
    UnsupportedOption,
)


class MastodonAdapter(Adapter):
    platform = Platform.MASTODON
    display_name = "Mastodon"
    implemented = True
    # Every status carries its own counts, and reading them back needs no extra
    # scope beyond the one used to post it.
    supports_metrics = True
    utm_medium = "social"
    # A status, not an article — see `Adapter.hosts_canonical`.
    hosts_canonical = False
    # The whole point of the platform is that the server is the user's choice,
    # so `instance_url` is an address an account holder aims Pulse at. See
    # `Adapter._send`.
    user_supplied_host = True
    credential_fields = (
        CredentialField(
            key="instance_url",
            label="Instance URL",
            help_text="e.g. https://fosstodon.org — the server your account is on.",
            secret=False,
        ),
        CredentialField(
            key="access_token",
            label="Access token",
            help_text=(
                "Preferences → Development → New application, with the "
                "write:statuses and read:accounts scopes."
            ),
        ),
        CredentialField(
            key="visibility",
            label="Visibility",
            help_text="public, unlisted, private or direct. Defaults to public.",
            secret=False,
            required=False,
        ),
    )

    #: Anything else would either not be seen (private, direct) or not federate
    #: the way an announcement should.
    _VISIBILITIES = ("public", "unlisted", "private", "direct")

    def _api(self, instance_url: str) -> str:
        """The API root, from whatever shape the user pasted.

        People paste ``fosstodon.org``, ``https://fosstodon.org/`` and
        ``https://fosstodon.org/@me`` in roughly equal measure; all three mean
        the same server.
        """
        raw = instance_url.strip()
        if "://" not in raw:
            raw = f"https://{raw}"
        parts = urlsplit(raw)
        if not parts.netloc:
            raise CredentialError(f"'{instance_url}' is not a usable instance URL")
        return f"{parts.scheme}://{parts.netloc}/api/v1"

    def _headers(self, token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def verify(self, credentials: dict[str, Any]) -> str:
        """Return the account, always fully qualified with its instance.

        ``acct`` comes back bare for a local account, and on a platform whose
        whole point is that there are several instances, ``@alice`` is not an
        answer to "which account is this".
        """
        instance_url, token = self._require(credentials, "instance_url", "access_token")
        resp = self._request(
            "GET",
            f"{self._api(instance_url)}/accounts/verify_credentials",
            headers=self._headers(token),
        )
        data = self._json_object(resp)
        if not data.get("acct"):
            raise CredentialError("Mastodon accepted the token but returned no account")
        # `acct` is bare for a local account, so spell out the instance either
        # way — the whole point of the platform is that there are several.
        host = urlsplit(self._api(instance_url)).netloc
        acct = data["acct"]
        return f"@{acct}" if "@" in acct else f"@{acct}@{host}"

    def preflight(self, request: PublishRequest) -> list[PreflightFinding]:
        """The same reading as Bluesky's, against a longer limit and a cheaper link.

        500 characters rather than 300, and a link is charged a flat 23 however
        long it is — so a piece that overflows Bluesky by a hundred characters
        routinely fits here, and saying so is the point of measuring both.
        """
        findings: list[PreflightFinding] = []

        text = request.excerpt or request.title
        if not text.strip():
            return [
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    "Nothing to post: this piece has neither an excerpt nor a "
                    "title, and a toot is the one or the other.",
                )
            ]

        wanted = formatting.social_budget(
            text=text,
            url=request.link,
            tags=request.tags,
            url_cost=formatting.MASTODON_LINK_COST,
        )
        if wanted > formatting.MASTODON_LIMIT:
            findings.append(
                PreflightFinding(
                    PREFLIGHT_WARNING,
                    f"{wanted - formatting.MASTODON_LIMIT} characters over the "
                    f"{formatting.MASTODON_LIMIT}-character default, so the "
                    "toot will be shortened. Instances can raise this limit; "
                    "Pulse composes to the default either way.",
                    limit=formatting.MASTODON_LIMIT,
                    actual=wanted,
                )
            )
        return findings

    def build_status(self, request: PublishRequest) -> str:
        """The text of the toot.

        The excerpt is the hook — it is written as a standalone summary, which
        is exactly what a 500-character post needs and what a truncated article
        body is not.
        """
        return formatting.compose_social(
            text=request.excerpt or request.title,
            url=request.link,
            tags=request.tags,
            limit=formatting.MASTODON_LIMIT,
            url_cost=formatting.MASTODON_LINK_COST,
        )

    def _visibility(self, credentials: dict[str, Any]) -> str:
        choice = str(credentials.get("visibility") or "").strip().lower()
        if choice and choice not in self._VISIBILITIES:
            raise CredentialError(
                f"'{choice}' is not a Mastodon visibility. Use one of: "
                + ", ".join(self._VISIBILITIES)
            )
        return choice or "public"

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Post a status. Refuses ``as_draft`` — Mastodon has no draft state."""
        instance_url, token = self._require(credentials, "instance_url", "access_token")
        if request.as_draft:
            raise UnsupportedOption(
                "Mastodon has no draft state — a status is either posted or it "
                "is not. Publish this without the draft option, or stage it "
                "somewhere that has drafts."
            )

        payload = {
            "status": self.build_status(request),
            "visibility": self._visibility(credentials),
            "language": "en",
        }

        resp = self._request(
            "POST",
            f"{self._api(instance_url)}/statuses",
            headers=self._headers(token, idempotency_key=request.idempotency_key),
            json_body=payload,
        )
        data = self._json_object(resp)

        status_id = data.get("id")
        if not status_id:
            raise PublishError(f"Mastodon returned no status id: {data}")

        return PublishResult(
            external_id=str(status_id),
            external_url=data.get("url") or data.get("uri") or "",
            extra={"visibility": data.get("visibility")},
        )

    def fetch_metrics(
        self, external_id: str, credentials: dict[str, Any]
    ) -> MetricsSnapshot:
        """Reblog, favourite and reply counts. Mastodon publishes no view count."""
        instance_url, token = self._require(credentials, "instance_url", "access_token")
        resp = self._request(
            "GET",
            f"{self._api(instance_url)}/statuses/{external_id}",
            headers=self._headers(token),
        )
        data = self._json_object(resp)
        # Mastodon reports no view count at all — deliberately, it does not
        # track them. `views=None` says "not reported", which is not zero.
        return MetricsSnapshot(
            reactions=data.get("favourites_count"),
            comments=data.get("replies_count"),
            shares=data.get("reblogs_count"),
        )


__all__ = ["MastodonAdapter"]
