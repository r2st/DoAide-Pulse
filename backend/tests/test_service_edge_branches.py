"""Degradation arms across the services nothing had walked.

Each test here pins the answer a service gives to input that is *legal but
unusual* — a variable list a hand-written template could carry, a platform the
cadence table has not caught up with, an article with no tags. None of them is
an error path; all of them are the difference between a graceful answer and a
``KeyError`` in front of a reader.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.content import Content, ContentType
from app.models.publication import Platform
from app.models.template import ContentTemplate
from app.services import ai, cadence, digest, feeds, formats, mailer, repurpose, seo
from app.services import templates as template_service

# --------------------------------------------------------------------------- #
# seo.truncate_at_sentence                                                     #
# --------------------------------------------------------------------------- #


def test_the_whole_text_can_fit_even_when_the_raw_string_does_not():
    """The split drops the whitespace between sentences, so the kept pieces can
    total less than the input did.

    A model that indents or double-spaces its prose lands here: the loop runs to
    completion without ever breaking, and everything survives. The arm exists
    because ``len(text) > limit`` does not imply any piece is over it.
    """
    text = "Hi." + " " * 12 + "Yo."  # 18 raw chars, 7 once rejoined

    assert seo.truncate_at_sentence(text, 15) == "Hi. Yo."


def test_a_first_sentence_past_the_limit_falls_back_to_a_word_boundary():
    text = "An opening clause that keeps going well past any sane limit indeed."

    result = seo.truncate_at_sentence(text, 30)

    assert result.endswith("…")
    assert len(result) <= 30
    assert not result[:-1].endswith(" ")


def test_text_already_inside_the_limit_is_returned_untouched():
    assert seo.truncate_at_sentence("  Short.  ", 100) == "Short."


# --------------------------------------------------------------------------- #
# cadence.cadence_for                                                          #
# --------------------------------------------------------------------------- #


def test_a_platform_the_table_has_not_caught_up_with_gets_a_safe_default(monkeypatch):
    """The guard the docstring promises: a new enum member must not crash.

    Simulated by removing an entry rather than inventing a member, because the
    situation being defended against is exactly "somebody added to the enum and
    not to the table".
    """
    table = dict(cadence.CADENCES)
    table.pop(Platform.DEVTO)
    monkeypatch.setattr(cadence, "CADENCES", table)

    fallback = cadence.cadence_for(Platform.DEVTO)

    assert fallback.platform is Platform.DEVTO
    assert fallback.max_per_week == cadence._DEFAULT_CADENCE_TEMPLATE.max_per_week
    assert "No platform-specific guidance" in fallback.rationale
    # And it is still describable, which is what the calendar sidebar needs.
    assert cadence.describe(Platform.DEVTO)["platform"] == "devto"


def test_a_string_platform_resolves_the_same_as_the_member():
    assert cadence.cadence_for("devto") == cadence.cadence_for(Platform.DEVTO)


# --------------------------------------------------------------------------- #
# templates.resolve                                                            #
# --------------------------------------------------------------------------- #


def test_a_malformed_variable_list_is_skipped_rather_than_crashing():
    """``variables`` is a JSON column. A hand-edited row can hold anything.

    A bare string and a nameless object are both things the API would refuse but
    the database will happily store, and neither should take the renderer down.
    """
    template = ContentTemplate(
        name="Weekly",
        body_template="Hello {{who}}.",
        variables=[
            "not-an-object",
            {"label": "nameless"},
            {"name": "", "default": "ignored"},
            {"name": "who", "default": "world"},
        ],
    )

    context, missing = template_service.resolve(template)

    assert context["who"] == "world"
    assert missing == []
    assert "not-an-object" not in context


def test_a_required_variable_with_nothing_behind_it_is_reported_missing():
    template = ContentTemplate(
        name="Weekly",
        body_template="Hello {{who}}.",
        variables=[{"name": "who", "required": True}],
    )

    context, missing = template_service.resolve(template)

    assert missing == ["who"]
    assert context["who"] == ""


def test_a_supplied_value_beats_the_declared_default():
    template = ContentTemplate(
        name="Weekly",
        body_template="Hello {{who}}.",
        variables=[{"name": "who", "default": "world"}],
    )

    context, _ = template_service.resolve(template, {"who": "  reader  "})

    assert context["who"] == "reader"


# --------------------------------------------------------------------------- #
# repurpose._build_prompt                                                      #
# --------------------------------------------------------------------------- #


def test_the_repurpose_prompt_omits_facts_the_piece_does_not_have():
    """A draft with no excerpt and no tags must not put empty labels in the prompt.

    ``Tags: `` with nothing after it reads to a model as "the tags are blank",
    which is a worse instruction than not mentioning tags at all.
    """
    bare = Content(
        project_id=1,
        content_type=ContentType.TUTORIAL,
        title="A piece",
        slug="a-piece",
        body_markdown="Body.",
        excerpt=None,
        tags=[],
    )

    messages = repurpose._build_prompt(bare, "Body.")
    prompt = messages[-1]["content"]

    assert "Title: A piece" in prompt
    assert "Excerpt:" not in prompt
    assert "Tags:" not in prompt


def test_the_repurpose_prompt_includes_them_when_they_are_there():
    full = Content(
        project_id=1,
        content_type=ContentType.TUTORIAL,
        title="A piece",
        slug="a-piece",
        body_markdown="Body.",
        excerpt="A short summary.",
        tags=["python", "testing"],
    )

    prompt = repurpose._build_prompt(full, "Body.")[-1]["content"]

    assert "Excerpt: A short summary." in prompt
    assert "Tags: python, testing" in prompt


# --------------------------------------------------------------------------- #
# feeds.fetch                                                                  #
# --------------------------------------------------------------------------- #


def test_an_unreachable_feed_becomes_a_feed_error_naming_the_cause():
    """The transport failure is wrapped, so callers catch one exception type."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    client = httpx.Client(transport=httpx.MockTransport(_refuse))
    with client, pytest.raises(feeds.FeedError) as excinfo:
        feeds.fetch("https://example.com/feed.xml", client=client)

    assert "Could not reach that feed" in str(excinfo.value)
    assert "ConnectError" in str(excinfo.value)


def test_a_caller_supplied_client_is_left_open_for_reuse():
    """``own_client`` decides who closes it — a shared client must survive."""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=(
                    b'<?xml version="1.0"?><rss version="2.0"><channel>'
                    b"<title>T</title><item><title>One</title>"
                    b"<link>https://example.com/1</link></item></channel></rss>"
                ),
                headers={"content-type": "application/rss+xml"},
            )
        )
    )
    with client:
        feeds.fetch("https://example.com/feed.xml", client=client)
        assert not client.is_closed


# --------------------------------------------------------------------------- #
# mailer.send                                                                  #
# --------------------------------------------------------------------------- #


def test_a_text_only_message_carries_no_html_alternative(monkeypatch):
    """``html`` is optional; passing nothing must not add an empty part."""
    sent: list = []

    class _FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("app.services.mailer.settings.smtp_host", "mail.example.com")
    monkeypatch.setattr("app.services.mailer.settings.smtp_user", "")
    monkeypatch.setattr("app.services.mailer.settings.smtp_use_ssl", False)
    monkeypatch.setattr("app.services.mailer.settings.smtp_starttls", False)
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)

    assert mailer.send(to="a@example.com", subject="Hi", body="Plain only.") is True

    message = sent[0]
    assert not message.is_multipart()
    assert message.get_content().strip() == "Plain only."


# --------------------------------------------------------------------------- #
# digest.render_text                                                           #
# --------------------------------------------------------------------------- #


def test_a_published_piece_with_no_url_is_still_listed():
    """Some platforms do not hand back a URL. The line still has to render."""
    built = digest.Digest(
        user_id=1,
        email="a@example.com",
        name="A",
        since=digest.datetime(2026, 1, 1),
        until=digest.datetime(2026, 1, 8),
        published=[
            {"content_id": 1, "title": "No link", "platforms": ["mastodon"], "url": None},
            {
                "content_id": 2,
                "title": "With link",
                "platforms": ["devto"],
                "url": "https://example.com/2",
            },
        ],
    )

    text = digest.render_text(built)

    assert "- No link — mastodon" in text
    assert "https://example.com/2" in text
    # Exactly one URL line: the piece without one contributes none.
    assert text.count("https://example.com") == 1


# --------------------------------------------------------------------------- #
# ai.extract_json_object                                                       #
# --------------------------------------------------------------------------- #


def test_a_nested_object_is_counted_by_depth_not_by_the_first_brace():
    parsed = ai.extract_json_object('prose {"outer": {"inner": {"deep": 1}}} more prose')

    assert parsed == {"outer": {"inner": {"deep": 1}}}


def test_the_largest_candidate_wins_over_a_later_smaller_one():
    """A scratchpad sketches, then answers — but not always in that order."""
    raw = 'Plan: {"title": "the real answer", "body": "a long finished thing"} then {"ok": 1}'

    assert ai.extract_json_object(raw)["title"] == "the real answer"


def test_a_non_dict_candidate_does_not_displace_a_real_one():
    assert ai.extract_json_object('{"a": 1} and then [1, 2, 3]') == {"a": 1}


def test_a_stray_closing_brace_is_scratchpad_noise():
    assert ai.extract_json_object('} leftover {"a": 1}') == {"a": 1}


def test_nothing_parseable_is_none():
    assert ai.extract_json_object("no object here at all") is None
    assert ai.extract_json_object("") is None


# --------------------------------------------------------------------------- #
# formats: the splitter's interaction with the thread repair pass              #
# --------------------------------------------------------------------------- #


def test_normalising_an_over_long_thread_truncates_to_the_ceiling():
    body = "\n\n".join(f"Post {i}." for i in range(formats.MAX_THREAD_POSTS + 5))

    posts = formats.parse_thread(formats.normalize_thread(body))

    assert len(posts) == formats.MAX_THREAD_POSTS
