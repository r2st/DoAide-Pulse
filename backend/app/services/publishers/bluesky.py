"""Bluesky adapter.

Like Mastodon, a free token-authenticated write API with a developer-heavy
audience; unlike Mastodon, the protocol underneath is AT rather than
ActivityPub, and that shows in three places worth reading before touching this
file.

**Sessions, not tokens.** There is no long-lived API key. An *app password*
(Settings → Privacy and security → App passwords — not the account password) is
exchanged for a short-lived ``accessJwt`` on every publish. That is one extra
request per post, and it is why :meth:`verify` costs nothing beyond the
handshake it already has to do.

**Links need facets.** Bluesky stores the post text as plain UTF-8 and carries
the markup alongside it as *facets*, each pointing at a byte range. Get the
offsets wrong — count characters instead of bytes, which is exactly what Python
string slicing invites — and the link either does not resolve or highlights the
wrong span. Any non-ASCII character before the URL shifts them, so an em dash in
the excerpt is enough to break it. :func:`link_facets` is the whole of that, and
it is tested against text with multi-byte characters in front of the link.

**300 graphemes, links counted in full.** No t.co shortening: a 90-character
canonical URL really does consume 90 of the 300.

API: https://docs.bsky.app/docs/api/com-atproto-repo-create-record
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

_DEFAULT_SERVICE = "https://bsky.social"

#: The record type for an ordinary post.
_COLLECTION = "app.bsky.feed.post"


def link_facets(text: str, url: str) -> list[dict[str, Any]]:
    """Facets marking *url* inside *text* as a link.

    Offsets are **byte** offsets into the UTF-8 encoding, not string indices.
    The two agree only while the text is pure ASCII, and generated copy is full
    of em dashes and smart quotes, so the difference is the normal case rather
    than the edge case.

    Returns an empty list when the URL is not present, which is what happens
    when the composer dropped it — better a post with a plain link than a facet
    pointing at the wrong bytes.
    """
    if not url or url not in text:
        return []
    start = len(text[: text.index(url)].encode("utf-8"))
    return [
        {
            "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        }
    ]


class BlueskyAdapter(Adapter):
    platform = Platform.BLUESKY
    display_name = "Bluesky"
    implemented = True
    supports_metrics = True
    utm_medium = "social"
    # A post, not an article: 300 characters carrying a link to the real thing.
    hosts_canonical = False
    # `service_url` defaults to bsky.social but exists so a self-hosted PDS can
    # be named, which makes it an address an account holder chooses. See
    # `Adapter._send`.
    user_supplied_host = True
    credential_fields = (
        CredentialField(
            key="handle",
            label="Handle",
            help_text="e.g. yourname.bsky.social",
            secret=False,
        ),
        CredentialField(
            key="app_password",
            label="App password",
            help_text=(
                "Settings → Privacy and security → App passwords. Not your "
                "account password."
            ),
        ),
        CredentialField(
            key="service_url",
            label="PDS URL",
            help_text="Only for a self-hosted PDS. Defaults to https://bsky.social.",
            secret=False,
            required=False,
        ),
    )

    def _service(self, credentials: dict[str, Any]) -> str:
        return str(credentials.get("service_url") or _DEFAULT_SERVICE).strip().rstrip("/")

    def _session(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Trade the app password for an access JWT and the account's DID."""
        handle, password = self._require(credentials, "handle", "app_password")
        resp = self._request(
            "POST",
            f"{self._service(credentials)}/xrpc/com.atproto.server.createSession",
            headers={"Content-Type": "application/json"},
            json_body={"identifier": handle.lstrip("@"), "password": password},
        )
        data = self._json_object(resp)
        if not data.get("accessJwt") or not data.get("did"):
            raise CredentialError(
                "Bluesky accepted the handshake but returned no session"
            )
        return data

    def verify(self, credentials: dict[str, Any]) -> str:
        """Open a session and return the handle it resolved to.

        Falls back to the handle the user typed when the session omits one, so a
        working credential never verifies to an empty display name.
        """
        session = self._session(credentials)
        return f"@{session.get('handle') or credentials.get('handle')}"

    def build_text(self, request: PublishRequest) -> str:
        """The post body. 300 graphemes, link counted at its real length."""
        return formatting.compose_social(
            text=request.excerpt or request.title,
            url=request.link,
            tags=request.tags,
            limit=formatting.BLUESKY_LIMIT,
            # No url_cost override: Bluesky does not shorten links.
        )

    def preflight(self, request: PublishRequest) -> list[PreflightFinding]:
        """What 300 graphemes will do to this piece, before it goes.

        Bluesky is the destination where the gap between what is on screen and
        what arrives is widest: an eight-hundred-word article becomes a hook
        and a link, and the link is charged at its real length because nothing
        here shortens it. The post always fits — ``compose_social`` sees to
        that — so the only useful thing to say is *how much* was cut, and to
        say it while the piece can still be edited.
        """
        findings: list[PreflightFinding] = []

        if request.as_draft:
            # The same refusal `publish` raises, made before an attempt is spent
            # on it. A row that fails on this is a row nobody had to queue.
            findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    "Bluesky has no draft state — a post is either live or it "
                    "does not exist.",
                )
            )

        text = request.excerpt or request.title
        if not text.strip():
            findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    "Nothing to post: this piece has neither an excerpt nor a "
                    "title, and Bluesky posts the one or the other.",
                )
            )
            return findings

        wanted = formatting.social_budget(
            text=text, url=request.link, tags=request.tags
        )
        if wanted > formatting.BLUESKY_LIMIT:
            findings.append(
                PreflightFinding(
                    PREFLIGHT_WARNING,
                    f"{wanted - formatting.BLUESKY_LIMIT} characters over "
                    f"Bluesky's {formatting.BLUESKY_LIMIT}, so the post will be "
                    "shortened to fit. The link is kept and the hashtags go "
                    "first; edit the excerpt to choose what survives.",
                    limit=formatting.BLUESKY_LIMIT,
                    actual=wanted,
                )
            )
        return findings

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Create a post record. Refuses ``as_draft`` — Bluesky has no draft state."""
        if request.as_draft:
            raise UnsupportedOption(
                "Bluesky has no draft state — a post is either live or it does "
                "not exist. Publish this without the draft option, or stage it "
                "somewhere that has drafts."
            )

        session = self._session(credentials)
        text = self.build_text(request)

        record: dict[str, Any] = {
            "$type": _COLLECTION,
            "text": text,
            # An RFC-3339 stamp with a Z suffix; Bluesky rejects a naive one.
            "createdAt": datetime.now(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            "langs": ["en"],
        }
        facets = link_facets(text, request.link or "")
        if facets:
            record["facets"] = facets

        resp = self._request(
            "POST",
            f"{self._service(credentials)}/xrpc/com.atproto.repo.createRecord",
            headers={
                "Authorization": f"Bearer {session['accessJwt']}",
                "Content-Type": "application/json",
            },
            json_body={
                "repo": session["did"],
                "collection": _COLLECTION,
                "record": record,
            },
        )
        data = self._json_object(resp)

        uri = data.get("uri")
        if not uri:
            raise PublishError(f"Bluesky returned no record uri: {data}")

        # at://did:plc:xxxx/app.bsky.feed.post/3k... — the last segment is the
        # rkey, which is what the web URL is built from. The handle is used
        # rather than the DID because it is the form that stays readable.
        handle = session.get("handle") or str(credentials.get("handle", "")).lstrip("@")
        rkey = uri.rsplit("/", 1)[-1]

        return PublishResult(
            # The AT URI, not the rkey: it is what getPosts takes, and it is
            # stable regardless of what the account renames itself to.
            external_id=uri,
            external_url=f"https://bsky.app/profile/{handle}/post/{rkey}",
            extra={"cid": data.get("cid")},
        )

    def fetch_metrics(
        self, external_id: str, credentials: dict[str, Any]
    ) -> MetricsSnapshot:
        """Reply, repost and like counts. Bluesky publishes no view count.

        A post that has been deleted, or that this account can no longer see,
        comes back as an empty snapshot rather than an error: there is nothing to
        retry, and the poller should move on quietly.
        """
        session = self._session(credentials)
        resp = self._request(
            "GET",
            f"{self._service(credentials)}/xrpc/app.bsky.feed.getPosts",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            params={"uris": external_id},
        )
        posts = self._json_object(resp).get("posts") or []
        if not posts:
            # Deleted, or the account lost access to it. Not an error worth
            # retrying — an empty snapshot records "nothing to see".
            return MetricsSnapshot()
        post = posts[0]
        # Bluesky publishes no view count.
        return MetricsSnapshot(
            reactions=post.get("likeCount"),
            comments=post.get("replyCount"),
            shares=post.get("repostCount"),
        )


__all__ = ["BlueskyAdapter", "link_facets"]
