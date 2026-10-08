"""The rendering engine: substitution, and the decisions around it.

These are unit tests against ``app.services.templates`` with a hand-built model
object rather than a database row — the render path never touches the session,
and keeping it that way is worth asserting by construction.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.content import ContentType
from app.models.template import ContentTemplate, TemplateMode
from app.models.trigger import TriggerKind
from app.services import templates as tmpl
from app.services.signals import TriggerSignal


def make_template(body="", title="", variables=None, mode=TemplateMode.LITERAL):
    return ContentTemplate(
        name="Test",
        mode=mode,
        content_type=ContentType.ANNOUNCEMENT,
        title_template=title,
        body_template=body,
        variables=variables or [],
    )


def var(name, **kwargs):
    return {"name": name, "label": name, "description": "", "default": "", **kwargs}


# --------------------------------------------------------------------------- #
# Substitution                                                                 #
# --------------------------------------------------------------------------- #


def test_a_declared_variable_is_filled_from_the_supplied_value():
    template = make_template(body="Shipped {{version}}.", variables=[var("version")])

    result = tmpl.render(template, {"version": "2.1"})

    assert result.body == "Shipped 2.1."
    assert result.filled == ["version"]


def test_whitespace_inside_the_braces_is_allowed():
    template = make_template(body="{{ version }}", variables=[var("version")])

    assert tmpl.render(template, {"version": "9"}).body == "9"


def test_a_supplied_value_beats_the_declared_default():
    template = make_template(
        body="{{channel}}", variables=[var("channel", default="stable")]
    )

    assert tmpl.render(template, {"channel": "beta"}).body == "beta"
    assert tmpl.render(template, {}).body == "stable"


def test_a_blank_supplied_value_falls_through_to_the_default():
    """An empty form field is "I didn't fill this in", not "make it empty"."""
    template = make_template(
        body="{{channel}}", variables=[var("channel", default="stable")]
    )

    assert tmpl.render(template, {"channel": "   "}).body == "stable"


def test_a_required_variable_with_no_value_anywhere_is_reported_missing():
    template = make_template(
        body="{{version}}", variables=[var("version", required=True)]
    )

    result = tmpl.render(template, {})

    assert result.missing == ["version"]
    assert result.is_complete is False


def test_a_required_variable_with_a_default_is_not_missing():
    template = make_template(
        body="{{version}}", variables=[var("version", required=True, default="1.0")]
    )

    result = tmpl.render(template, {})

    assert result.missing == []
    assert result.body == "1.0"


def test_a_render_never_raises_on_an_undeclared_placeholder():
    """Write-time validation is what stops this; a mid-publish crash is worse."""
    template = make_template(body="Hello {{nobody}}!")

    assert tmpl.render(template, {}).body == "Hello !"


def test_substitution_runs_one_pass_so_a_value_cannot_inject_a_placeholder():
    """The security property: a stranger's RSS body is text, not template."""
    template = make_template(body="{{quote}}", variables=[var("quote")])

    result = tmpl.render(template, {"quote": "literally {{secret}} here"})

    assert result.body == "literally {{secret}} here"


def test_a_value_longer_than_the_limit_is_truncated():
    template = make_template(body="{{blob}}", variables=[var("blob")])

    result = tmpl.render(template, {"blob": "x" * (tmpl.VALUE_LIMIT + 500)})

    assert len(result.body) == tmpl.VALUE_LIMIT


def test_the_title_is_collapsed_to_one_line():
    template = make_template(title="  {{a}}   {{b}} ", variables=[var("a"), var("b")])

    assert tmpl.render(template, {"a": "Pulse", "b": "2.0"}).title == "Pulse 2.0"


# --------------------------------------------------------------------------- #
# Empty lines                                                                  #
# --------------------------------------------------------------------------- #


def test_a_line_that_was_only_scaffolding_and_an_empty_value_is_dropped():
    template = make_template(
        body="We shipped it.\nRead more: {{link}}", variables=[var("link")]
    )

    assert tmpl.render(template, {}).body == "We shipped it."


def test_that_same_line_survives_when_the_value_is_there():
    template = make_template(
        body="We shipped it.\nRead more: {{link}}", variables=[var("link")]
    )

    result = tmpl.render(template, {"link": "https://example.com"})

    assert result.body == "We shipped it.\nRead more: https://example.com"


def test_a_line_with_the_authors_own_words_is_kept_even_when_empty():
    """Literal text is a choice; only pure scaffolding is disposable."""
    template = make_template(body="Changelog {{version}}", variables=[var("version")])

    assert tmpl.render(template, {}).body == "Changelog"


@pytest.mark.parametrize(
    "line",
    ["Read more: {{link}}", "Version — {{link}}", "- {{link}}", "({{link}})"],
)
def test_a_label_for_a_missing_value_goes_with_it(line):
    template = make_template(body=f"Shipped.\n{line}", variables=[var("link")])

    assert tmpl.render(template, {}).body == "Shipped."


@pytest.mark.parametrize(
    "line", ["We shipped {{link}} today", "Docs at {{link}}", "Changelog {{link}}"]
)
def test_a_sentence_containing_a_missing_value_keeps_its_words(line):
    """The line is prose the author wrote, not a caption for the blank."""
    template = make_template(body=f"Shipped.\n{line}", variables=[var("link")])

    assert tmpl.render(template, {}).body != "Shipped."


def test_dropping_lines_does_not_leave_a_run_of_blanks():
    template = make_template(
        body="One.\n\n{{gone}}\n\n{{alsogone}}\n\nTwo.",
        variables=[var("gone"), var("alsogone")],
    )

    assert tmpl.render(template, {}).body == "One.\n\nTwo."


def test_a_multiline_value_disables_line_dropping_rather_than_guessing():
    """Once the lines no longer correspond, deleting one would delete content."""
    template = make_template(
        body="Changes:\n{{items}}\nLink: {{link}}",
        variables=[var("items"), var("link")],
    )

    result = tmpl.render(template, {"items": "- a\n- b"})

    assert "- a\n- b" in result.body
    # `Link:` kept, because alignment was abandoned — the honest failure mode.
    assert "Link:" in result.body


# --------------------------------------------------------------------------- #
# Built-ins                                                                    #
# --------------------------------------------------------------------------- #


def test_project_builtins_resolve_without_being_declared(project):
    template = make_template(body="{{project.name}} — {{project.url}}")

    result = tmpl.render(template, {}, project=project)

    assert result.body == "Pulse — https://pulse.example.com"


def test_project_url_falls_back_to_the_repo_when_there_is_no_site(project):
    project.live_url = ""
    template = make_template(body="{{project.url}}")

    assert tmpl.render(template, {}, project=project).body == (
        "https://github.com/r2st/DoAide-Pulse"
    )


def test_date_builtins_use_the_supplied_moment():
    template = make_template(body="{{date.today}} / {{date.long}} / {{date.month}}")

    result = tmpl.render(template, {}, now=datetime(2026, 8, 1, tzinfo=UTC))

    assert result.body == "2026-08-01 / 1 August 2026 / August"


def test_signal_builtins_carry_a_trigger_firing_into_the_template():
    signal = TriggerSignal(
        kind=TriggerKind.RSS,
        source="RSS Changelog",
        headline="v2.1 is out",
        summary="Faster everything.",
        items=("Fixed the parser", "Added templates"),
        url="https://example.com/v2.1",
    )
    template = make_template(
        body="{{signal.headline}}\n\n{{signal.items}}\n\nvia {{signal.source}}"
    )

    result = tmpl.render(template, {}, signal=signal)

    assert result.body == (
        "v2.1 is out\n\n- Fixed the parser\n- Added templates\n\nvia RSS Changelog"
    )


def test_signal_builtins_render_empty_without_a_trigger():
    template = make_template(body="Update.\nSee: {{signal.url}}")

    assert tmpl.render(template, {}).body == "Update."


def test_a_supplied_value_cannot_override_a_builtin(project):
    """That would be working around the template rather than filling it in."""
    template = make_template(body="{{project.name}}")

    result = tmpl.render(template, {"project.name": "Not Pulse"}, project=project)

    assert result.body == "Pulse"


def test_every_advertised_builtin_actually_resolves(project):
    """The editor's insert menu is built from BUILTINS; nothing in it may be a lie."""
    context = tmpl.builtin_context(project=project)

    assert set(context) == set(tmpl.BUILTINS)


# --------------------------------------------------------------------------- #
# Write-time validation helpers                                                #
# --------------------------------------------------------------------------- #


def test_unknown_placeholders_are_found():
    assert tmpl.unknown_placeholders(["version"], "{{version}} {{versoin}}") == [
        "versoin"
    ]


def test_builtins_do_not_count_as_unknown():
    assert tmpl.unknown_placeholders([], "{{project.name}} {{date.today}}") == []


def test_declared_names_may_not_shadow_a_builtin_namespace():
    assert tmpl.reserved_names(["project", "date.today", "version"]) == [
        "date.today",
        "project",
    ]


def test_placeholders_are_returned_in_first_seen_order_without_duplicates():
    assert tmpl.placeholders("{{b}} {{a}}", "{{a}} {{c}}") == ["b", "a", "c"]


# --------------------------------------------------------------------------- #
# Excerpt                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# Heading\n\nThe first real paragraph.", "The first real paragraph."),
        ("- a bullet\n\nProse.", "a bullet"),
        ("", ""),
        ("> quoted\n\n![img](x)\n\nActual words.", "Actual words."),
    ],
)
def test_excerpt_finds_the_first_prose_block(body, expected):
    assert tmpl.excerpt_from(body) == expected


def test_a_long_excerpt_is_cut_at_a_word_boundary():
    result = tmpl.excerpt_from("word " * 100, limit=40)

    assert len(result) <= 40
    assert result.endswith("…")
    assert "wor…" not in result
