"""Buttondown adapter — the email newsletter destination.

An API key in a header, Markdown in the body, and a free tier that does not
expire. Like WordPress and unlike LinkedIn, there is no OAuth dance, which is
what makes email one of the few channels Herald can support honestly without a
registered app nobody can complete.

**Email is the one destination that cannot be taken back.** A blog post can be
deleted, a tweet can be deleted, a WordPress post can be reverted to draft — a
sent newsletter is in somebody's inbox and stays there. Two consequences run
through this adapter:

* **The send is explicit at every layer.** Buttondown's ``2026-04-01`` API made
  ``draft`` the default status precisely because callers were sending mail they
  meant to stage, and it gates a real send behind a confirmation header as well.
  This adapter never relies on either default: it names the status every time,
  and only sends the confirmation header when it is genuinely publishing.
* **The retry must not double-send.** :meth:`Adapter._request` already refuses
  to replay a POST except on 429 and 503, but "the platform never processed it"
  is a judgement made from the outside and a timeout is ambiguous. So the piece's
  slug goes up as the email's slug: Buttondown scopes slugs uniquely per
  newsletter, so a replay of a request that *did* land is rejected by the server
  rather than delivered twice. Best-effort, and much better than nothing when
  the failure mode is a duplicate in five hundred inboxes.

API: https://docs.buttondown.com/api-emails-create
"""
from __future__ import annotations

from typing import Any

from app.models.publication import Platform
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    CredentialField,
    PublishError,
    PublishRequest,
    PublishResult,
)

_API = "https://api.buttondown.com/v1"

#: Buttondown reads a leading ``---`` as YAML front matter and rejects the email
#: with ``body_contains_frontmatter`` rather than guessing. A Herald body starts
#: with a heading or a paragraph, but a Markdown horizontal rule is a legal way
#: to open a piece and a 400 at send time is a bad way to find that out.
_FRONT_MATTER_START = "---"

#: Buttondown's own name for "yes, I meant it". Required to actually send, and
#: to accept a body the front-matter check would otherwise refuse.
_CONFIRM_HEADER = "X-Buttondown-Live-Dangerously"


class ButtondownAdapter(Adapter):
    platform = Platform.BUTTONDOWN
    display_name = "Buttondown"
    implemented = True
    # Not "referral" and not "syndication": an email subscriber arrives from a
    # channel the reader opted into, and folding that in with a link on a social
    # post throws away the only segmentation Herald gets for free.
    utm_medium = "email"
    # The archive page is an issue of a newsletter, not the article's home.
    hosts_canonical = False
    # Buttondown reports opens and clicks in its own dashboard, but the shape of
    # the analytics on the email object is undocumented. Reporting nothing is
    # honest; reporting a guessed zero would make every issue look unread.
    supports_metrics = False
    caveat = (
        "Publishing here sends the newsletter to your subscribers, and an email "
        "cannot be unsent. Use 'Publish as draft' to stage it in Buttondown and "
        "send it from there."
    )
    credential_fields = (
        CredentialField(
            key="api_key",
            label="API key",
            help_text="buttondown.com/settings/programming — not your password.",
        ),
    )

    def _headers(self, api_key: str, *, confirm: bool = False) -> dict[str, str]:
        headers = {
            # The trailing space after "Token" is part of the scheme.
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if confirm:
            headers[_CONFIRM_HEADER] = "true"
        return headers

    def build_body(self, request: PublishRequest) -> str:
        """The email body: Markdown, cover on top, one link at the end.

        Markdown rather than HTML because Buttondown renders it itself and its
        own templating sits on top of the rendered output — handing it HTML
        opts out of the newsletter's styling for no gain.
        """
        blocks: list[str] = []
        if request.cover_image_url:
            blocks.append(f"![{request.title}]({request.cover_image_url})")
        blocks.append(request.body_markdown.strip())
        link = request.link
        if link:
            blocks.append(f"[Read it on the web]({link})")
        return "\n\n".join(block for block in blocks if block)

    def build_payload(self, request: PublishRequest) -> dict[str, Any]:
        """The ``POST /v1/emails`` body.

        ``status`` is always named. Buttondown defaults it to ``draft`` now, but
        an adapter that sends live mail should not be one upstream default away
        from doing the opposite of what it was asked.
        """
        payload: dict[str, Any] = {
            "subject": request.title,
            "body": self.build_body(request),
            "status": "draft" if request.as_draft else "about_to_send",
        }
        # The duplicate-send guard described in the module docstring.
        if request.slug:
            payload["slug"] = request.slug
        return payload

    def needs_confirmation(self, request: PublishRequest) -> bool:
        """Whether this request has to carry the "yes, I meant it" header.

        True for a real send, and for the one draft that would otherwise be
        refused: a body that opens with what Buttondown reads as front matter.
        """
        if not request.as_draft:
            return True
        return self.build_body(request).lstrip().startswith(_FRONT_MATTER_START)

    def verify(self, credentials: dict[str, Any]) -> str:
        (api_key,) = self._require(credentials, "api_key")
        resp = self._request(
            "GET", f"{_API}/newsletters", headers=self._headers(api_key)
        )
        data = self._json(resp) or {}
        # The endpoint is paginated, but a bare list is what older keys return.
        results = data.get("results", data) if isinstance(data, dict) else data
        if not results:
            raise CredentialError(
                "Buttondown accepted the key but it is not attached to a "
                "newsletter. Create one at buttondown.com first."
            )
        first = results[0] if isinstance(results, list) else results
        if not isinstance(first, dict):
            raise CredentialError(f"Buttondown returned an unexpected newsletter: {first}")
        return first.get("name") or first.get("username") or "Buttondown"

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        (api_key,) = self._require(credentials, "api_key")

        resp = self._request(
            "POST",
            f"{_API}/emails",
            headers=self._headers(
                api_key, confirm=self.needs_confirmation(request)
            ),
            json_body=self.build_payload(request),
        )
        data = self._json(resp) or {}

        email_id = data.get("id")
        if not email_id:
            raise PublishError(f"Buttondown returned no email id: {data}")

        # `absolute_url` is the public archive page. A draft has no archive yet,
        # so fall back to the dashboard, which is where the user has to go to
        # send it anyway.
        url = data.get("absolute_url") or data.get("web_url") or (
            f"https://buttondown.com/emails/{email_id}"
        )
        return PublishResult(
            external_id=str(email_id),
            external_url=url,
            extra={"status": data.get("status"), "slug": data.get("slug")},
        )


__all__ = ["ButtondownAdapter"]
