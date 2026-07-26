"""Adapter formatting and the registry.

The network is never touched: what is under test is the translation layer, which
is where the platform-specific bugs actually live.
"""
from __future__ import annotations

import pytest

from app.models.publication import Platform
from app.services import publishers
from app.services.publishers import formatting
from app.services.publishers.base import NotImplementedAdapter, PublishRequest
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.twitter import TwitterAdapter
from app.services.publishers.wordpress import WordPressAdapter


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        body_markdown=(
            "## Why\n\nHerald watches your repos and writes the posts.\n\n"
            "```python\nprint('hello')\n```\n\n"
            "## How\n\nIt uses a chain of free models with a circuit breaker in "
            "front of them, so one dead provider never stops the pipeline.\n"
        ),
        excerpt="Herald watches your repos and writes the posts for you.",
        meta_description="Herald automates developer marketing end to end.",
        tags=["python", "dev-tools", "AI", "automation", "extra", "sixth"],
        canonical_url="https://herald.example.com/blog/automating",
        project_url="https://herald.example.com",
        project_name="Herald",
    )


def test_registry_reports_what_actually_works():
    implemented = set(publishers.implemented_platforms())
    assert implemented == {Platform.DEVTO, Platform.MEDIUM}
    # Every enum member has an adapter, finished or not.
    assert len(publishers.all_adapters()) == len(list(Platform))
    # Working adapters sort first for the settings page.
    assert publishers.all_adapters()[0].implemented


def test_capabilities_never_leak_credential_values():
    for capability in publishers.capabilities():
        for field in capability["credential_fields"]:
            assert set(field) == {"key", "label", "help_text", "secret", "required"}


def test_unknown_platform_raises():
    with pytest.raises(publishers.UnknownPlatform):
        publishers.get_adapter("myspace")


def test_unfinished_adapters_refuse_clearly(request_):
    for adapter in (HashnodeAdapter(), LinkedInAdapter(), TwitterAdapter(), WordPressAdapter()):
        with pytest.raises(NotImplementedAdapter) as exc:
            adapter.publish(request_, {})
        assert "not finished" in str(exc.value)


def test_markdown_to_html_keeps_code_fences(request_):
    html = formatting.to_html(request_.body_markdown)
    assert "<h2>" in html
    # The language hint has to survive — it is most of why a code block in a
    # technical post is worth anything, and it is what Medium's own Markdown
    # handling drops (see the medium adapter docstring).
    assert '<pre><code class="language-python">' in html


def test_plain_text_drops_markup_but_keeps_paragraphs(request_):
    text = formatting.to_plain_text(request_.body_markdown)
    assert "##" not in text
    assert "```" not in text
    assert "\n\n" in text


def test_tweet_stays_under_the_limit_with_a_long_url():
    long_url = "https://example.com/" + "x" * 300
    tweet = formatting.truncate_for_tweet("word " * 200, url=long_url)
    body = tweet[: -len(long_url) - 1]
    # The URL is reserved at its t.co length, not its real one.
    assert len(body) + formatting.TCO_LENGTH + 1 <= formatting.TWEET_LIMIT


def test_tweet_never_cuts_mid_word():
    tweet = formatting.truncate_for_tweet("alpha beta gamma delta " * 40)
    assert len(tweet) <= formatting.TWEET_LIMIT
    assert tweet.endswith("…")
    assert not tweet[:-1].endswith(" ")


def test_thread_fits_every_tweet(request_):
    thread = TwitterAdapter().build_thread(request_)
    assert 1 < len(thread) <= 5
    assert all(len(t) <= formatting.TWEET_LIMIT for t in thread)
    assert request_.canonical_url in thread[0]


def test_linkedin_commentary_is_plain_text_and_bounded(request_):
    text = LinkedInAdapter().build_commentary(request_)
    assert "##" not in text
    assert len(text) <= formatting.LINKEDIN_HARD_LIMIT
    assert request_.canonical_url in text


def test_tag_normalization_respects_platform_rules():
    # Dev.to: alphanumeric only, four max.
    assert formatting.normalize_tags(
        ["dev-tools", "AI", "python", "automation", "fifth"], limit=4
    ) == ["devtools", "ai", "python", "automation"]
    # Medium allows spaces.
    assert formatting.normalize_tags(
        ["developer marketing"], limit=5, allow_spaces=True
    ) == ["developer marketing"]


def test_front_matter_skips_empty_values_and_escapes_quotes():
    block = formatting.front_matter(
        {"title": 'A "quoted" title', "published": True, "tags": ["a", "b"], "skip": ""}
    )
    assert 'title: "A \\"quoted\\" title"' in block
    assert "published: true" in block
    assert 'tags: ["a", "b"]' in block
    assert "skip" not in block


def test_hashnode_payload_shape(request_):
    payload = HashnodeAdapter().build_payload(request_, "pub-123")
    assert payload["publicationId"] == "pub-123"
    assert payload["contentMarkdown"] == request_.body_markdown
    assert payload["originalArticleURL"] == request_.canonical_url
    assert all(set(t) == {"slug", "name"} for t in payload["tags"])
    assert len(payload["tags"]) <= 5


def test_wordpress_payload_and_auth_header(request_):
    adapter = WordPressAdapter()
    assert adapter.api_root("https://blog.example.com/") == (
        "https://blog.example.com/wp-json/wp/v2"
    )
    # Application passwords are pasted with spaces; they must be stripped.
    assert adapter.auth_header("dev", "abcd efgh ijkl") == adapter.auth_header(
        "dev", "abcdefghijkl"
    )
    payload = adapter.build_payload(request_)
    assert payload["status"] == "publish"
    assert "<h2>" in payload["content"]


def test_devto_metadata_only_sends_canonical_when_present(request_, monkeypatch):
    """Forem 422s on an empty canonical_url, so it must be omitted, not blanked."""
    sent: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": 1, "url": "https://dev.to/x/y"}

    adapter = DevToAdapter()
    monkeypatch.setattr(
        adapter, "_request", lambda *a, **kw: (sent.update(kw), FakeResponse())[1]
    )

    adapter.publish(request_, {"api_key": "k"})
    assert sent["json_body"]["article"]["canonical_url"] == request_.canonical_url

    no_canonical = PublishRequest(
        title="t", body_markdown="b", excerpt="e", meta_description=""
    )
    adapter.publish(no_canonical, {"api_key": "k"})
    assert "canonical_url" not in sent["json_body"]["article"]
    assert "description" not in sent["json_body"]["article"]


def test_missing_credential_is_a_credential_error(request_):
    from app.services.publishers.base import CredentialError

    with pytest.raises(CredentialError) as exc:
        DevToAdapter().publish(request_, {})
    assert "api_key" in str(exc.value)
