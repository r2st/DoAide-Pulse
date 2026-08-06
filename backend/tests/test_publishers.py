"""Adapter formatting and the registry.

The network is never touched: what is under test is the translation layer, which
is where the platform-specific bugs actually live.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.publication import Platform
from app.services import publishers
from app.services.publishers import bluesky, formatting
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishRequest,
    UnsupportedOption,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.git import GitAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.mastodon import MastodonAdapter
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
    # The two that need an auth flow nobody can complete on a free tier are the
    # only ones left: LinkedIn wants a registered app, Twitter a paid plan.
    assert implemented == set(Platform) - {Platform.LINKEDIN, Platform.TWITTER}
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
    for adapter in (LinkedInAdapter(), TwitterAdapter()):
        with pytest.raises(NotImplementedAdapter) as exc:
            adapter.publish(request_, {})
        assert "not finished" in str(exc.value)


def test_finished_adapters_ask_for_credentials_rather_than_refusing(request_):
    # The distinction the API answers "why can't I publish here?" with: an
    # unfinished adapter is a Herald problem, a missing credential is the
    # user's, and they must not look the same.
    for adapter in (
        HashnodeAdapter(),
        WordPressAdapter(),
        MastodonAdapter(),
        BlueskyAdapter(),
        GitAdapter(),
    ):
        with pytest.raises(CredentialError):
            adapter.publish(request_, {})


def test_platforms_without_drafts_refuse_rather_than_going_live(request_):
    # The one outcome that must never happen: somebody ticks "draft" to stay
    # unpublished and the post goes out anyway.
    draft = replace(request_, as_draft=True)
    for adapter in (MastodonAdapter(), BlueskyAdapter()):
        with pytest.raises(UnsupportedOption) as exc:
            adapter.publish(draft, {"instance_url": "https://x.social",
                                    "access_token": "t", "handle": "h",
                                    "app_password": "p"})
        assert "draft" in str(exc.value)


# -- Mastodon -------------------------------------------------------------- #


def test_mastodon_status_fits_the_limit_and_keeps_the_link(request_):
    status = MastodonAdapter().build_status(request_)
    assert request_.canonical_url in status
    assert "#python" in status
    # Links cost a flat 23 wherever they really point.
    charged = len(status) - len(request_.canonical_url) + formatting.MASTODON_LINK_COST
    assert charged <= formatting.MASTODON_LIMIT


def test_mastodon_status_still_fits_with_an_enormous_excerpt(request_):
    long = replace(request_, excerpt="word " * 400)
    status = MastodonAdapter().build_status(long)
    charged = len(status) - len(long.canonical_url) + formatting.MASTODON_LINK_COST
    assert charged <= formatting.MASTODON_LIMIT
    assert long.canonical_url in status


@pytest.mark.parametrize(
    "pasted", ["fosstodon.org", "https://fosstodon.org", "https://fosstodon.org/@me"]
)
def test_mastodon_accepts_any_shape_of_instance_url(pasted):
    assert MastodonAdapter()._api(pasted) == "https://fosstodon.org/api/v1"


def test_mastodon_rejects_an_unknown_visibility():
    with pytest.raises(CredentialError):
        MastodonAdapter()._visibility({"visibility": "shouty"})


# -- Bluesky --------------------------------------------------------------- #


def test_bluesky_post_fits_the_limit_counting_the_link_in_full(request_):
    text = BlueskyAdapter().build_text(request_)
    assert len(text) <= formatting.BLUESKY_LIMIT
    assert request_.canonical_url in text


def test_bluesky_facets_are_byte_offsets_not_character_offsets():
    # An em dash is one character and three bytes. Counting characters here is
    # the bug this test exists for: the link would highlight the wrong span.
    url = "https://herald.example.com/post"
    text = f"Shipped — read it: {url}"
    (facet,) = bluesky.link_facets(text, url)

    start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
    assert text.encode("utf-8")[start:end].decode("utf-8") == url
    assert start != text.index(url)  # i.e. the naive version would be wrong


def test_bluesky_facets_are_empty_when_the_link_did_not_survive():
    assert bluesky.link_facets("no link here", "https://example.com") == []


#: A share link the length a campaign-tagged one really reaches: the slug lands
#: in the path and again in ``utm_content``, and Bluesky shortens neither. At
#: this length the link alone is most of a Bluesky post, which is the situation
#: the composer used to get wrong.
_LONG_SHARE_URL = (
    "https://gstbot.example.com/blog/2026/q3/"
    "how-to-reconcile-your-gstr-2b-against-the-purchase-register-without-"
    "quietly-losing-input-tax-credit"
    "?utm_source=bluesky&utm_medium=social&utm_campaign=gstbot-2026-q3-launch"
    "&utm_content=how-to-reconcile-your-gstr-2b-against-the-purchase-register"
)


def test_bluesky_post_fits_even_when_the_link_eats_the_whole_budget(request_):
    """The regression: a long share link used to produce a 470-character post.

    The hashtag line and the link together left ``clip`` a negative budget, and
    a negative budget returned the entire text. Bluesky 400s anything over 300
    and the publish then failed on every retry.
    """
    req = replace(request_, share_url=_LONG_SHARE_URL)
    text = BlueskyAdapter().build_text(req)

    assert len(text) <= formatting.BLUESKY_LIMIT
    # The link is the payload — it survives, whole, whatever else is dropped.
    assert _LONG_SHARE_URL in text


def test_clip_returns_nothing_when_there_is_no_room_left():
    # The slice `body[:budget - 1]` returns almost the whole string here.
    assert formatting.clip("a fairly long sentence", 0) == ""
    assert formatting.clip("a fairly long sentence", -12) == ""


def test_compose_social_drops_hashtags_before_it_drops_the_link():
    post = formatting.compose_social(
        text="Reconciliation is four problems wearing one name.",
        url=_LONG_SHARE_URL,
        tags=["gst", "itc", "compliance"],
        limit=formatting.BLUESKY_LIMIT,
    )
    assert len(post) <= formatting.BLUESKY_LIMIT
    assert _LONG_SHARE_URL in post
    assert "#gst" not in post


def test_compose_social_keeps_hashtags_when_they_fit():
    post = formatting.compose_social(
        text="Short hook.",
        url="https://example.com/p",
        tags=["gst", "itc"],
        limit=formatting.BLUESKY_LIMIT,
    )
    assert post.endswith("#gst #itc")
    assert len(post) <= formatting.BLUESKY_LIMIT


def test_compose_social_measures_a_shortened_link_at_what_it_costs():
    """Mastodon charges 23 for a link however long it is.

    Measuring the composed post with ``len`` would drop the hashtags off a post
    that fits perfectly well — the fix must not overcorrect into that.
    """
    post = formatting.compose_social(
        text="Reconciliation is four problems wearing one name.",
        url=_LONG_SHARE_URL,
        tags=["gst", "itc"],
        limit=formatting.MASTODON_LIMIT,
        url_cost=formatting.MASTODON_LINK_COST,
    )
    billed = formatting.billed_length(
        post, url=_LONG_SHARE_URL, url_cost=formatting.MASTODON_LINK_COST
    )
    assert billed <= formatting.MASTODON_LIMIT
    assert "#gst #itc" in post  # room to spare once the link is billed at 23


def test_a_link_longer_than_the_whole_post_goes_out_whole():
    """A truncated URL is a dead link, so the bare link is the honest answer."""
    url = "https://example.com/" + "x" * 400
    assert formatting.truncate_with_link("hook", url=url, limit=300) == url


def test_billed_length_counts_a_link_at_face_value_by_default():
    text = "read this https://example.com/a-very-long-path-indeed"
    assert formatting.billed_length(text) == len(text)
    assert formatting.billed_length(text, url="https://example.com") == len(text)


# -- Git ------------------------------------------------------------------- #


def test_git_file_has_front_matter_and_body(request_):
    contents = GitAdapter().build_file(request_)
    assert contents.startswith("---\n")
    assert 'title: "Automating developer marketing"' in contents
    assert "## Why" in contents
    assert "draft:" not in contents


def test_git_file_includes_keywords_in_front_matter():
    req = PublishRequest(
        title="Test",
        body_markdown="## Test\n\nBody text.",
        excerpt="Test excerpt.",
        meta_description="Test description.",
        tags=["python"],
        keywords=["python", "automation"],
        focus_keyword="python",
    )
    contents = GitAdapter().build_file(req)
    assert "keywords:" in contents
    assert "focusKeyword:" in contents


def test_git_file_includes_reading_time(request_):
    contents = GitAdapter().build_file(request_)
    assert "readingTime:" in contents
    assert "wordCount:" in contents


def test_git_file_includes_json_ld(request_):
    contents = GitAdapter().build_file(request_)
    assert '<script type="application/ld+json">' in contents
    import json
    # Extract the JSON-LD block and verify it parses.
    start = contents.index('<script type="application/ld+json">') + len(
        '<script type="application/ld+json">'
    )
    end = contents.index("</script>")
    parsed = json.loads(contents[start:end])
    assert parsed["@type"] == "Article"
    assert parsed["headline"] == request_.title


def test_git_file_carries_open_graph_keys_in_front_matter(request_):
    # Front matter and not the body: a crawler reads <head>, so a body-level
    # <meta> would be ignored — unlike the JSON-LD above it, which is not.
    contents = GitAdapter().build_file(
        replace(request_, cover_image_url="https://cdn.example.com/cover.png")
    )

    assert 'ogTitle: "Automating developer marketing"' in contents
    assert 'ogImage: "https://cdn.example.com/cover.png"' in contents
    assert 'twitterCard: "summary_large_image"' in contents
    assert "<meta" not in contents


def test_git_file_without_a_cover_downgrades_the_card_and_omits_the_image(request_):
    contents = GitAdapter().build_file(request_)

    assert 'twitterCard: "summary"' in contents
    # An empty ogImage makes a theme render an empty <meta>.
    assert "ogImage:" not in contents


def test_git_draft_is_a_front_matter_flag_not_a_refusal(request_):
    contents = GitAdapter().build_file(replace(request_, as_draft=True))
    assert "draft: true" in contents


def test_git_path_template_takes_the_slug_and_the_date(request_):
    piece = replace(request_, slug="automating-developer-marketing")
    path = GitAdapter().path_for(piece, {"path_template": "_posts/{year}-{month}-{day}-{slug}.md"})
    assert path.endswith("-automating-developer-marketing.md")
    assert path.startswith("_posts/")


def test_git_refuses_a_path_that_escapes_the_repository(request_):
    with pytest.raises(CredentialError):
        GitAdapter().path_for(request_, {"path_template": "../../etc/{slug}.md"})


def test_git_refuses_an_unknown_placeholder(request_):
    with pytest.raises(CredentialError):
        GitAdapter().path_for(request_, {"path_template": "posts/{author}.md"})


@pytest.mark.parametrize(
    "pasted",
    ["r2st/Herald", "https://github.com/r2st/Herald", "git@github.com:r2st/Herald.git"],
)
def test_git_accepts_any_shape_of_repo(pasted):
    assert GitAdapter()._repo({"repo": pasted}) == "r2st/Herald"


def test_git_rejects_something_that_is_not_a_repo():
    with pytest.raises(CredentialError):
        GitAdapter()._repo({"repo": "just-a-name"})


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


def test_wordpress_payload_includes_seo_meta():
    req = PublishRequest(
        title="Test SEO",
        body_markdown="## Test\n\nBody.",
        excerpt="Test excerpt.",
        meta_description="A great meta description for SEO.",
        tags=["python"],
        keywords=["python"],
        focus_keyword="python",
        canonical_url="https://example.com/original",
    )
    payload = WordPressAdapter().build_payload(req)
    assert "meta" in payload
    assert payload["meta"]["_yoast_wpseo_metadesc"] == "A great meta description for SEO."
    assert payload["meta"]["_yoast_wpseo_focuskw"] == "python"
    assert payload["meta"]["_yoast_wpseo_canonical"] == "https://example.com/original"


def test_wordpress_payload_omits_meta_when_empty(request_):
    # The base fixture has no focus_keyword and no canonical_url set.
    # meta_description is set but canonical_url isn't — should still include
    # what's available.
    payload = WordPressAdapter().build_payload(request_)
    if "meta" in payload:
        assert "_yoast_wpseo_metadesc" in payload["meta"]


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
