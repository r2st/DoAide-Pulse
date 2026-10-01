"""Campaign tagging: the URL helper, and what build_request does with it.

The invariant worth guarding above all others is that ``canonical_url`` is never
tagged. Everything else here is about not damaging a URL — or a body — that was
already correct.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.publication import Platform
from app.services import publishing_service, utm

PARAMS = {
    "source": "devto",
    "medium": "syndication",
    "campaign": "herald",
    "content": "shipping-fast",
}


def params_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# -- the URL helper -------------------------------------------------------- #


def test_tag_appends_the_four_parameters():
    tagged = utm.tag("https://herald.example.com/", **PARAMS)
    assert params_of(tagged) == {
        "utm_source": "devto",
        "utm_medium": "syndication",
        "utm_campaign": "herald",
        "utm_content": "shipping-fast",
    }


def test_tag_keeps_existing_query_and_fragment():
    tagged = utm.tag("https://example.com/a?ref=x&b=1#install", **PARAMS)
    assert params_of(tagged)["ref"] == "x"
    assert params_of(tagged)["b"] == "1"
    assert params_of(tagged)["utm_source"] == "devto"
    assert tagged.endswith("#install")


def test_a_hand_written_utm_parameter_is_not_overwritten():
    # Somebody pasted a link they had already tagged. Their value is the
    # intentional one; ours is a default.
    tagged = utm.tag("https://example.com/?utm_source=newsletter", **PARAMS)
    assert params_of(tagged)["utm_source"] == "newsletter"
    # The parameters they did *not* set are still filled in.
    assert params_of(tagged)["utm_campaign"] == "herald"


@pytest.mark.parametrize(
    "url", [None, "", "mailto:hi@example.com", "/relative/path", "not a url"]
)
def test_non_http_urls_are_returned_untouched(url):
    assert utm.tag(url, **PARAMS) == url


def test_blank_parameters_are_omitted_rather_than_sent_empty():
    tagged = utm.tag("https://example.com/", source="devto", campaign="herald", content="")
    assert "utm_content" not in params_of(tagged)


def test_host_of_ignores_www_port_and_case():
    assert utm.host_of("https://WWW.Example.com:8443/path") == "example.com"
    assert utm.host_of(None) == ""


# -- Markdown bodies ------------------------------------------------------- #


def test_body_links_to_the_project_are_tagged():
    body = "Try [Pulse](https://herald.example.com/signup) today."
    out = utm.tag_markdown_links(body, host="herald.example.com", **PARAMS)
    assert "utm_source=devto" in out
    assert out.startswith("Try [Pulse](https://herald.example.com/signup?")


def test_third_party_links_are_left_alone():
    body = "See [the docs](https://fastapi.tiangolo.com/) for more."
    assert utm.tag_markdown_links(body, host="herald.example.com", **PARAMS) == body


def test_urls_inside_fenced_code_are_not_rewritten():
    body = (
        "Install it:\n\n"
        "```bash\ncurl [x](https://herald.example.com/install.sh)\n```\n\n"
        "Then read [the guide](https://herald.example.com/guide).\n"
    )
    out = utm.tag_markdown_links(body, host="herald.example.com", **PARAMS)
    assert "install.sh)" in out  # untouched inside the fence
    assert "guide?utm_source=devto" in out


def test_a_bracketed_url_survives_the_rewrite():
    """A `)` in the address is part of it, not the end of the link.

    Reading the address as "up to the first `)`" stopped a character early and
    then consumed the link's own `)` as the closing one, publishing a dead link
    on the user's own domain — the one host this rewriter touches at all.
    """
    body = "Read [the API notes](https://herald.example.com/docs/api_(v2)) first."
    out = utm.tag_markdown_links(body, host="herald.example.com", **PARAMS)

    assert "/docs/api_(v2)?utm_source=devto" in out
    assert out.endswith(") first.")
    assert "api_(v2?" not in out


def test_an_unbalanced_bracket_is_left_alone_rather_than_mangled():
    """Not tagging costs attribution; mangling costs the reader the page."""
    body = "Try [this](https://herald.example.com/x(y) now."
    assert utm.tag_markdown_links(body, host="herald.example.com", **PARAMS) == body


def test_link_titles_survive_the_rewrite():
    body = '[Pulse](https://herald.example.com/ "The tool")'
    out = utm.tag_markdown_links(body, host="herald.example.com", **PARAMS)
    assert out.endswith('"The tool")')
    assert "utm_campaign=herald" in out


# -- build_request --------------------------------------------------------- #


@pytest.fixture
def content(db, project):
    project.live_url = "https://herald.example.com"
    project.utm_campaign = ""
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        title="Shipping fast",
        slug="shipping-fast",
        body_markdown="Read more at [the site](https://herald.example.com/blog).",
        excerpt="We shipped.",
        meta_description="We shipped.",
        tags=["python"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_canonical_url_is_never_tagged(db, content):
    content.canonical_url = "https://herald.example.com/blog/shipping-fast"
    db.commit()

    request = publishing_service.build_request(content, platform=Platform.MEDIUM)

    # The rel=canonical stays exactly what was published...
    assert request.canonical_url == "https://herald.example.com/blog/shipping-fast"
    # ...while the link a reader is sent down carries the campaign.
    assert "utm_source=medium" in request.share_url
    assert request.share_url.startswith(request.canonical_url)


def test_share_url_falls_back_to_the_project_url(db, content):
    request = publishing_service.build_request(content, platform=Platform.DEVTO)
    assert params_of(request.share_url) == {
        "utm_source": "devto",
        "utm_medium": "syndication",
        "utm_campaign": content.project.slug,  # blank utm_campaign → slug
        "utm_content": "shipping-fast",
    }


def test_utm_content_is_capped_so_the_link_cannot_swallow_the_post(db, content):
    """A 300-character title makes a 300-character slug, and it lands twice.

    Once in the path and once in ``utm_content`` — which on Bluesky, where the
    whole post is 300 characters and links are not shortened, is the entire
    budget spent on the query string of the post's own link.
    """
    content.slug = "reconciling-gstr-2b-against-the-purchase-register-" + "x" * 250
    db.commit()

    request = publishing_service.build_request(content, platform=Platform.BLUESKY)
    tagged = params_of(request.share_url)["utm_content"]

    assert len(tagged) <= publishing_service._UTM_CONTENT_MAX
    assert content.slug.startswith(tagged)
    # Still long enough to tell two posts apart in a report.
    assert tagged.startswith("reconciling-gstr-2b-against-the-purchase-register")


def test_utm_content_never_ends_on_a_stray_hyphen(db, content):
    content.slug = "a" * 59 + "-trailing-word"
    db.commit()

    request = publishing_service.build_request(content, platform=Platform.DEVTO)
    assert not params_of(request.share_url)["utm_content"].endswith("-")


def test_a_short_slug_is_left_exactly_as_it_is(db, content):
    request = publishing_service.build_request(content, platform=Platform.DEVTO)
    assert params_of(request.share_url)["utm_content"] == "shipping-fast"


def test_the_medium_reflects_the_kind_of_platform(db, content):
    social = publishing_service.build_request(content, platform=Platform.TWITTER)
    assert params_of(social.share_url)["utm_medium"] == "social"


def test_body_links_are_tagged_per_platform(db, content):
    request = publishing_service.build_request(content, platform=Platform.DEVTO)
    assert "https://herald.example.com/blog?utm_source=devto" in request.body_markdown


def test_disabling_utm_leaves_every_url_alone(db, content):
    content.project.utm_enabled = False
    db.commit()

    request = publishing_service.build_request(content, platform=Platform.DEVTO)

    assert "utm_" not in (request.share_url or "")
    assert "utm_" not in request.body_markdown
    assert request.project_url == "https://herald.example.com"


def test_no_platform_means_no_invented_source(db, content):
    # The SEO panel builds a request without naming a destination. A utm_source
    # Pulse made up would look like data.
    request = publishing_service.build_request(content)
    assert "utm_" not in (request.share_url or "")
    assert request.idempotency_key is None


def test_the_idempotency_key_is_stable_per_content_and_platform(db, content):
    first = publishing_service.build_request(content, platform=Platform.DEVTO)
    second = publishing_service.build_request(content, platform=Platform.DEVTO)
    other = publishing_service.build_request(content, platform=Platform.MEDIUM)

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key != other.idempotency_key
