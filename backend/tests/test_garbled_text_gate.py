"""A foreign-script run spliced into the copy holds a piece back.

Free-tier models occasionally emit a token from another script mid-sentence. The
run is not a translation of anything nearby and means nothing — the sampler
slipped. Six pieces reached Dev.to and Bluesky under the user's name carrying
one: ``selects a publish time <CYRILLIC>``, ``the front<CJK> end``, ``a dynamic,
repeatable scenario<MALAYALAM> framework``.

Nothing downstream looked. The JSON parsed, ``too_thin_to_store`` counted enough
words, ``looks_like_reasoning`` saw no planning markers, and both SEO gates were
blind to it by construction: the score measures structure and
``blocking_issues`` measures completeness, and a body with a Cyrillic noun in
paragraph three is structurally perfect and complete. Confidence came back 0.85.

So this is a third gate, on the one thing the other two do not read — the words.
It holds the piece for review and names the runs, rather than deleting them:
stripping the run is usually right and sometimes silently wrong, because the
model may have dropped the English word it meant to write.

The gate is deliberately biased toward review. A long article that legitimately
quotes one foreign term is held back too — the cost is a human glance at a piece
that names exactly which characters to look at, and the alternative is what
production did.

The literals below are the actual runs from the affected rows, each named for the
script it came from — the name is what makes a failure readable, since one of
these glyphs on its own says very little about which row it broke.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.services import ai, content_generator, content_pipeline
from app.services.content_generator import GeneratedContent

# The runs actually found in the affected production rows.
CYRILLIC = "стратегия"  # strategy
CJK_FRONT = "蛅"
CJK_SPECIFY = "指定"
MALAYALAM = "ഡ്"  # a consonant plus its virama
DEVANAGARI = "भोजन"  # food
TELUGU = "కీయ"
GUJARATI = "ંગ્ર"

# Long enough to clear the word floor and to keep the foreign share far under
# `_MULTILINGUAL_SHARE`, so what each test injects is the only thing wrong.
_BODY = (
    "## Retries in Herald\n\n"
    "Retries in Herald are the part people ask about first. "
    + "The retry path is careful about retries and about what a retry costs. " * 45
)


# --------------------------------------------------------------------------- #
# stray_script_runs                                                            #
# --------------------------------------------------------------------------- #


def test_clean_english_has_no_stray_runs():
    assert ai.stray_script_runs(_BODY) == []


def test_empty_text_has_no_stray_runs():
    assert ai.stray_script_runs("") == []


@pytest.mark.parametrize(
    "run",
    [CYRILLIC, CJK_FRONT, CJK_SPECIFY, MALAYALAM, DEVANAGARI, TELUGU, GUJARATI],
    ids=["cyrillic", "cjk-front", "cjk-specify", "malayalam", "devanagari",
         "telugu", "gujarati"],
)
def test_every_run_seen_in_production_is_caught(run):
    """One regression per affected row, named by the script that broke it."""
    assert ai.stray_script_runs(f"{_BODY} and selects a publish time {run}.") == [run]


def test_a_run_is_caught_when_it_is_welded_onto_an_english_word():
    """How these actually appear: no space, mid-token.

    ``the front<CJK> end`` — which is why a whitespace-delimited word check would
    not have found them.
    """
    assert ai.stray_script_runs(f"{_BODY} through the front{CJK_FRONT} end.") == [
        CJK_FRONT
    ]


def test_a_combining_mark_stays_with_its_consonant():
    """The Malayalam run is two codepoints; splitting it would misreport it."""
    runs = ai.stray_script_runs(f"{_BODY} a repeatable scenario{MALAYALAM} framework.")

    assert runs == [MALAYALAM]
    assert len(runs[0]) == 2


def test_distinct_runs_are_all_reported():
    text = f"{_BODY} time {CYRILLIC}. The front{CJK_FRONT} end and message{CJK_SPECIFY}."

    assert ai.stray_script_runs(text) == [CYRILLIC, CJK_FRONT, CJK_SPECIFY]


def test_a_repeated_run_is_reported_once():
    text = f"{_BODY} time {CYRILLIC} and again {CYRILLIC} here."

    assert ai.stray_script_runs(text) == [CYRILLIC]


def test_the_limit_caps_the_list():
    text = _BODY + " ".join(f"word{chr(0x4e00 + i)}" for i in range(30))

    assert len(ai.stray_script_runs(text, limit=4)) == 4


# --------------------------------------------------------------------------- #
# What must *not* fire                                                         #
# --------------------------------------------------------------------------- #


def test_typography_is_not_a_stray_run():
    """Every one of these models emits these, and they are correct."""
    text = "Herald’s retries — which never double-post … are careful."

    assert ai.stray_script_runs(text) == []


def test_accented_latin_is_not_a_stray_run():
    assert ai.stray_script_runs("Bücher, naïve, café, Zürich.") == []


def test_greek_letters_are_not_stray_runs():
    """"a lambda folded over sigma" is prose a developer writes on purpose."""
    text = f"{_BODY} The λ folds over σ with α and π."

    assert ai.stray_script_runs(text) == []


def test_text_written_in_another_script_is_not_contaminated_by_one():
    """A translation is not a glitch, and this is the wrong gate to fail it at."""
    hindi = " ".join([DEVANAGARI] * 20)

    assert ai.stray_script_runs(f"{hindi} Herald retries.") == []


def test_the_share_is_measured_over_the_whole_text_not_per_run():
    """A single long foreign passage is intentional; a sprinkle is not."""
    sprinkled = f"{_BODY} time {CYRILLIC}."
    written = " ".join([CYRILLIC] * 40) + " Herald."

    assert ai.stray_script_runs(sprinkled) == [CYRILLIC]
    assert ai.stray_script_runs(written) == []


# --------------------------------------------------------------------------- #
# The gate, through the pipeline                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def auto_project(db, project, connect, monkeypatch):
    """A project that would auto-publish anything it is handed."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    db.commit()
    connect("devto")
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])
    return project


def _generated(**overrides) -> GeneratedContent:
    base = {
        "title": "Retries in Herald",
        "body_markdown": _BODY,
        "excerpt": "How retries work.",
        "meta_description": (
            "How retries work in Herald, why a retry never double-posts, and "
            "what the backoff actually does when a platform is down."
        ),
        "keywords": ["retries"],
        "tags": ["python"],
        "focus_keyword": "retries",
        "confidence": 0.99,
    }
    base.update(overrides)
    return GeneratedContent(**base)


@pytest.fixture
def writes(monkeypatch):
    def _install(generated: GeneratedContent) -> None:
        monkeypatch.setattr(content_generator, "generate", lambda *a, **k: generated)

    return _install


def _route(db, project):
    return content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )


def test_a_clean_piece_still_auto_publishes(db, auto_project, writes):
    """The control. Without this the tests below pass for the wrong reason."""
    writes(_generated())

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.garbled_runs == []


def test_a_garbled_body_goes_to_review_instead_of_publishing(
    db, auto_project, writes
):
    """The bug. Herald published six of these under the user's name."""
    writes(_generated(body_markdown=f"{_BODY} selects a publish time {CYRILLIC}."))

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW


def test_a_garbled_title_goes_to_review(db, auto_project, writes):
    """The title is the worst place for one and the most visible."""
    writes(_generated(title=f"Retries in Herald {CJK_SPECIFY}"))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_a_garbled_meta_description_goes_to_review(db, auto_project, writes):
    """Not derived from the body on every path, so it is checked on its own."""
    writes(_generated(meta_description=f"How retries work in Herald {DEVANAGARI}."))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_a_garbled_excerpt_goes_to_review(db, auto_project, writes):
    writes(_generated(excerpt=f"How retries work {TELUGU}."))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_the_gate_does_not_fire_on_a_clean_piece_with_typography(
    db, auto_project, writes
):
    """The false positive that would matter most: it would hold back everything."""
    writes(
        _generated(
            title="Herald’s retries — explained",
            body_markdown=f"{_BODY} Herald’s retries — never double-post…",
        )
    )

    assert _route(db, auto_project).status == content_pipeline.AUTO_PUBLISHED


def test_the_runs_reach_the_reviewer(db, auto_project, writes):
    """A held-back piece whose reason is only in the logs is a mystery in a queue."""
    writes(_generated(body_markdown=f"{_BODY} a publish time {CYRILLIC}."))

    routed = _route(db, auto_project)

    assert routed.garbled_runs == [CYRILLIC]
    assert routed.content.source["garbled_runs"] == [CYRILLIC]


def test_the_runs_reach_the_task_summary(db, auto_project, writes):
    writes(_generated(body_markdown=f"{_BODY} a publish time {CYRILLIC}."))

    summary = _route(db, auto_project).summary()

    assert summary["garbled_runs"] == [CYRILLIC]
    assert summary["status"] == content_pipeline.QUEUED_FOR_REVIEW


def test_a_clean_summary_carries_no_garbled_runs_key(db, auto_project, writes):
    """Same contract as ``dead_links`` and ``seo_errors``: absent, not empty."""
    writes(_generated())

    assert "garbled_runs" not in _route(db, auto_project).summary()


def test_the_runs_are_banked_even_when_something_else_held_the_piece(
    db, project, writes
):
    """Asked regardless of ``auto``, so a reviewer sees every reason at once."""
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    writes(_generated(body_markdown=f"{_BODY} a publish time {CYRILLIC}."))

    routed = _route(db, project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.garbled_runs == [CYRILLIC]
