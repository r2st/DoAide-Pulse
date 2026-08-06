"""Hashnode adapter.

Hashnode is GraphQL-only (https://gql.hashnode.com), which is the whole reason
it lagged the REST destinations. Two consequences shape this file:

* **Failure arrives with a 200.** GraphQL reports errors in the response body,
  not the status code, so the shared :meth:`_request` error translation sees a
  perfectly successful call. :meth:`_gql` is what re-derives the distinction —
  and it has to, because an expired token showing up as a generic
  :class:`PublishError` would be retried three times before anyone was told to
  reconnect.
* **Publishing needs a ``publicationId``.** A Hashnode account can own several
  blogs and the mutation will not guess. It is a credential field, and
  :meth:`verify` lists the ids the token can see so it can be found without
  digging through a dashboard URL.

Drafts go through a different mutation entirely (``createDraft``) rather than a
flag on the published one, so ``as_draft`` branches early.

API: https://apidocs.hashnode.com/
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

_API = "https://gql.hashnode.com"

_TAG_LIMIT = 5

_PUBLISH_POST = """
mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) {
    post { id slug url }
  }
}
"""

_CREATE_DRAFT = """
mutation CreateDraft($input: CreateDraftInput!) {
  createDraft(input: $input) {
    draft { id slug }
  }
}
"""

_ME = """
query Me {
  me {
    username
    publications(first: 10) {
      edges { node { id title url } }
    }
  }
}
"""

#: Error codes Hashnode returns for a token problem. Anything else is treated as
#: possibly transient and left to the retry budget.
_AUTH_CODES = {"UNAUTHENTICATED", "FORBIDDEN", "NOT_FOUND"}


class HashnodeAdapter(Adapter):
    platform = Platform.HASHNODE
    display_name = "Hashnode"
    implemented = True
    utm_medium = "syndication"
    # Hashnode's public API exposes post views only on the analytics dashboard,
    # not through the GraphQL schema.
    supports_metrics = False
    caveat = (
        "Needs the publication ID of the blog to post to. Connect the account "
        "and Herald lists the ones your token can see."
    )
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

    def _gql(self, token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One GraphQL call, with the errors turned back into exceptions.

        GraphQL answers 200 for a rejected token, a missing publication and a
        malformed query alike, so the shared HTTP translation in
        :meth:`Adapter._request` cannot see any of them. Splitting auth errors
        out here is what stops a stale token being retried three times before
        anyone is told to reconnect.
        """
        resp = self._request(
            "POST",
            _API,
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json_body={"query": query, "variables": variables},
        )
        body = resp.json() or {}

        errors = body.get("errors") or []
        if errors:
            message = "; ".join(
                str(error.get("message") or error) for error in errors
            )
            codes = {
                str((error.get("extensions") or {}).get("code") or "").upper()
                for error in errors
            }
            if codes & _AUTH_CODES:
                raise CredentialError(f"Hashnode rejected the request: {message}")
            raise PublishError(f"Hashnode returned an error: {message}")

        data = body.get("data")
        if not data:
            raise PublishError(f"Hashnode returned no data: {body}")
        return data

    def verify(self, credentials: dict[str, Any]) -> str:
        (token,) = self._require(credentials, "api_key")
        me = (self._gql(token, _ME, {}) or {}).get("me") or {}
        if not me.get("username"):
            raise CredentialError("Hashnode accepted the token but returned no account")

        # Listing the publications is the point: the id is a credential field,
        # and it is otherwise found by reading it out of a dashboard URL.
        publications = [
            edge["node"]
            for edge in ((me.get("publications") or {}).get("edges") or [])
            if edge.get("node")
        ]
        if not publications:
            return f"@{me['username']}"
        listed = ", ".join(f"{p.get('title')} ({p.get('id')})" for p in publications)
        return f"@{me['username']} — publications: {listed}"

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        token, publication_id = self._require(credentials, "api_key", "publication_id")
        payload = self.build_payload(request, publication_id)

        if request.as_draft:
            data = self._gql(token, _CREATE_DRAFT, {"input": payload})
            draft = ((data.get("createDraft") or {}).get("draft")) or {}
            draft_id = draft.get("id")
            if not draft_id:
                raise PublishError(f"Hashnode returned no draft: {data}")
            return PublishResult(
                external_id=str(draft_id),
                # A draft has no public address; the editor is the useful link.
                external_url=f"https://hashnode.com/draft/{draft_id}",
                extra={"slug": draft.get("slug"), "draft": True},
            )

        data = self._gql(token, _PUBLISH_POST, {"input": payload})
        post = ((data.get("publishPost") or {}).get("post")) or {}
        post_id = post.get("id")
        if not post_id:
            raise PublishError(f"Hashnode returned no post: {data}")

        return PublishResult(
            external_id=str(post_id),
            external_url=post.get("url", ""),
            extra={"slug": post.get("slug")},
        )


__all__ = ["HashnodeAdapter"]
