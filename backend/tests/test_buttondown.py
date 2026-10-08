"""The email newsletter destination.

Two things are worth testing here that are not worth testing on the blogging
adapters, and both come from the same fact: a sent email cannot be unsent. Does
a draft stay a draft, and can a retry send the same issue twice?
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.publication import Platform
from app.services import publishers
from app.services.publishers.base import (
    CredentialError,
    PublishError,
    PublishRequest,
)
from app.services.publishers.buttondown import ButtondownAdapter


@pytest.fixture
def adapter() -> ButtondownAdapter:
    return ButtondownAdapter()


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Pulse writes the posts now",
        body_markdown="## Why\n\nPulse watches your repos and writes the posts.",
        excerpt="Pulse watches your repos.",
        meta_description="Pulse automates developer marketing.",
        tags=["python", "automation"],
        slug="pulse-writes-the-posts-now",
        canonical_url="https://pulse.example.com/blog/writes",
        project_url="https://pulse.example.com",
        project_name="Pulse",
    )


class FakeResponse:
    """Just enough httpx.Response for an adapter."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def sent(adapter, monkeypatch):
    """Captures the outgoing call and replies with a plausible email object."""
    captured: dict = {}
    reply: dict = {
        "id": "em-1",
        "status": "about_to_send",
        "slug": "pulse-writes-the-posts-now",
        "absolute_url": "https://buttondown.com/pulse/archive/writes",
    }

    def fake_request(method, url, **kwargs):
        captured.update(kwargs, method=method, url=url)
        return FakeResponse(captured.get("_reply", reply))

    monkeypatch.setattr(adapter, "_request", fake_request)
    return captured


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def test_the_registry_knows_it_and_calls_it_email():
    resolved = publishers.get_adapter(Platform.BUTTONDOWN)

    assert resolved.implemented is True
    assert Platform.BUTTONDOWN in publishers.implemented_platforms()
    # Not "referral": an opted-in subscriber is its own kind of channel.
    assert resolved.utm_medium == "email"


def test_the_caveat_says_the_thing_that_cannot_be_undone():
    caveat = ButtondownAdapter().caveat.lower()

    assert "cannot be unsent" in caveat


def test_it_does_not_claim_metrics_it_cannot_report():
    """None is not zero — a guessed zero makes every issue look unread."""
    assert ButtondownAdapter().supports_metrics is False
    assert ButtondownAdapter().fetch_metrics("em-1", {"api_key": "k"}).views is None


# --------------------------------------------------------------------------- #
# The send / draft distinction                                                 #
# --------------------------------------------------------------------------- #


def test_publishing_sends_and_says_so_explicitly(adapter, request_):
    """Never one upstream default away from doing the opposite of the ask."""
    payload = adapter.build_payload(request_)

    assert payload["status"] == "about_to_send"
    assert payload["subject"] == request_.title


def test_a_draft_stays_a_draft(adapter, request_):
    payload = adapter.build_payload(replace(request_, as_draft=True))

    assert payload["status"] == "draft"


def test_a_real_send_carries_the_confirmation_header(adapter, request_, sent):
    adapter.publish(request_, {"api_key": "k"})

    assert sent["headers"]["X-Buttondown-Live-Dangerously"] == "true"
    assert sent["json_body"]["status"] == "about_to_send"


def test_a_draft_does_not_carry_it(adapter, request_, sent):
    """The header is the gate on sending. A draft has no business opening it."""
    adapter.publish(replace(request_, as_draft=True), {"api_key": "k"})

    assert "X-Buttondown-Live-Dangerously" not in sent["headers"]


def test_a_draft_opening_with_a_rule_still_gets_through(adapter, request_):
    """A leading '---' reads as front matter and is refused without the header.

    A horizontal rule is a legal way to open a piece, and a 400 at send time is
    a bad way to find that out.
    """
    rule_first = replace(request_, body_markdown="---\n\nStraight in.", as_draft=True)

    assert adapter.needs_confirmation(rule_first) is True
    # ...but only because of the body. An ordinary draft is unaffected.
    assert adapter.needs_confirmation(replace(request_, as_draft=True)) is False


def test_a_cover_image_does_not_disguise_front_matter(adapter, request_):
    """The check runs on the body actually sent, not the raw Markdown."""
    with_cover = replace(
        request_,
        body_markdown="---\n\nStraight in.",
        cover_image_url="https://img.example.com/c.png",
        as_draft=True,
    )

    # The image is now the first block, so nothing trips the front-matter rule.
    assert adapter.needs_confirmation(with_cover) is False


# --------------------------------------------------------------------------- #
# Not sending the same issue twice                                             #
# --------------------------------------------------------------------------- #


def test_the_slug_goes_up_as_a_duplicate_guard(adapter, request_):
    """Buttondown scopes slugs per newsletter, so a replay is refused server-side."""
    assert adapter.build_payload(request_)["slug"] == request_.slug


def test_a_piece_with_no_slug_omits_it_rather_than_sending_a_blank(adapter, request_):
    payload = adapter.build_payload(replace(request_, slug=""))

    assert "slug" not in payload


# --------------------------------------------------------------------------- #
# The body                                                                     #
# --------------------------------------------------------------------------- #


def test_the_body_is_markdown_not_html(adapter, request_):
    """Buttondown renders Markdown itself; HTML opts out of the newsletter styling."""
    body = adapter.build_body(request_)

    assert "## Why" in body
    assert "<h2>" not in body


def test_the_cover_leads_and_the_link_closes(adapter, request_):
    body = adapter.build_body(
        replace(request_, cover_image_url="https://img.example.com/c.png")
    )

    assert body.startswith("![Pulse writes the posts now](https://img.example.com/c.png)")
    assert body.rstrip().endswith(f"[Read it on the web]({request_.canonical_url})")


def test_a_piece_with_no_cover_and_no_link_is_just_the_body(adapter):
    bare = PublishRequest(
        title="t", body_markdown="Just this.", excerpt="", meta_description=""
    )

    assert adapter.build_body(bare) == "Just this."


# --------------------------------------------------------------------------- #
# Credentials and failure                                                      #
# --------------------------------------------------------------------------- #


def test_the_auth_header_keeps_the_space_after_token(adapter):
    assert adapter._headers("abc")["Authorization"] == "Token abc"


def test_a_missing_key_is_a_credential_error(adapter, request_):
    with pytest.raises(CredentialError) as exc:
        adapter.publish(request_, {})

    assert "api_key" in str(exc.value)


def test_verify_returns_the_newsletter_name(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *a, **kw: FakeResponse({"results": [{"name": "The Pulse Weekly"}]}),
    )

    assert adapter.verify({"api_key": "k"}) == "The Pulse Weekly"


def test_verify_accepts_a_bare_list_too(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: FakeResponse([{"username": "pulse"}])
    )

    assert adapter.verify({"api_key": "k"}) == "pulse"


def test_a_key_with_no_newsletter_is_a_credential_error(adapter, monkeypatch):
    """Valid key, nothing to publish to. Retrying will not fix it."""
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: FakeResponse({"results": []})
    )

    with pytest.raises(CredentialError):
        adapter.verify({"api_key": "k"})


def test_a_response_with_no_id_is_a_publish_error(adapter, request_, monkeypatch):
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: FakeResponse({"detail": "nope"})
    )

    with pytest.raises(PublishError):
        adapter.publish(request_, {"api_key": "k"})


def test_the_result_carries_the_archive_url(adapter, request_, sent):
    result = adapter.publish(request_, {"api_key": "k"})

    assert result.external_id == "em-1"
    assert result.external_url == "https://buttondown.com/pulse/archive/writes"
    assert result.extra["status"] == "about_to_send"


def test_a_draft_with_no_archive_falls_back_to_the_dashboard(adapter, request_, monkeypatch):
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: FakeResponse({"id": "em-9", "status": "draft"})
    )

    result = adapter.publish(replace(request_, as_draft=True), {"api_key": "k"})

    assert result.external_url == "https://buttondown.com/emails/em-9"
