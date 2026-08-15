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


# --------------------------------------------------------------------------- #
# stray_letter_splices — the other half of the same slip                       #
# --------------------------------------------------------------------------- #
#
# The script gate above exempts Greek and the Latin supplements on purpose, so
# that "a lambda folded over sigma", "Bücher" and "résumé" survive. The sampler
# slips inside those ranges too, and there the damage is a word rather than a
# run: nothing about the codepoint in "démontrated" differs from the one in
# "résumé". Two of these reached Dev.to and Bluesky and were *not* among the six
# the script gate caught, because every character in them was allowed.
#
# The literals are the real words from the affected production rows.

SPLICES = {
    "latin-e-diaeresis": ("rënd", "the cost‑of‑capital methodology rënd the base case"),
    "schwa": ("nətive", "invoices are uploaded via a nətive file picker"),
    "latin-a-diaeresis": ("stärker", "an alert in the stärker dashboard, AI draft ready"),
    "latin-e-acute": ("démontrated", "démontrated latency improvements translate directly"),
    "latin-i-acute": ("vísit", "the system vísit checks if the cache is still fresh"),
    "latin-o-diaeresis": ("tört", "a FastAPI endpoint creates a tört draft in the database"),
    "welded-onto-english": ("Slowämp", "## The Problem: Slowämp Real estate agents"),
    "vietnamese-a": ("hấp", "cross-references market data and flags any outasy hấp"),
    "vietnamese-o": ("theồng", "a toggle lets users switch between theồng of the three"),
    "greek-welded": ("α‑authentic", "ensuring only α‑authentic requests reach the database"),
    "greek-run": ("φω", "FastAPI is used to expose φω webhooks and preview routes"),
}


@pytest.mark.parametrize(
    ("word", "sentence"), list(SPLICES.values()), ids=list(SPLICES)
)
def test_every_splice_seen_in_production_is_caught(word, sentence):
    """One regression per affected row, named for the letter that broke it."""
    assert ai.stray_letter_splices(f"{_BODY} {sentence}.") == [word]


def test_clean_english_has_no_splices():
    assert ai.stray_letter_splices(_BODY) == []


def test_empty_text_has_no_splices():
    assert ai.stray_letter_splices("") == []


def test_distinct_splices_are_all_reported():
    text = f"{_BODY} a nətive picker, a tört draft, and the stärker dashboard."

    assert ai.stray_letter_splices(text) == ["nətive", "tört", "stärker"]


def test_a_repeated_splice_is_reported_once():
    text = f"{_BODY} a tört draft and another tört draft."

    assert ai.stray_letter_splices(text) == ["tört"]


def test_the_limit_caps_the_splice_list():
    text = _BODY + " ".join(f"wörd{chr(ord('a') + i)}" for i in range(20))

    assert len(ai.stray_letter_splices(text, limit=4)) == 4


def test_the_whole_word_is_returned_not_the_offending_letter():
    """"rënd" tells a reviewer what it was meant to say; "ë" tells them nothing."""
    runs = ai.stray_letter_splices(f"{_BODY} the methodology rënd the assumption.")

    assert runs == ["rënd"]
    assert "ë" not in runs


# --------------------------------------------------------------------------- #
# What must *not* fire                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "loanword",
    ["résumé", "résumés", "café", "naïve", "cliché", "façade", "déjà", "über"],
)
def test_words_english_actually_borrows_are_not_splices(loanword):
    """The production row that has these is a real post about job ads."""
    assert ai.stray_letter_splices(f"{_BODY} collects {loanword} into a pipeline.") == []


def test_a_lone_greek_letter_is_a_symbol_not_a_splice():
    """Same prose the script gate protects: "a lambda folded over sigma"."""
    assert ai.stray_letter_splices(f"{_BODY} The λ folds over σ with α and π.") == []


def test_typography_is_not_a_splice():
    text = f"{_BODY} Herald’s retries — which never double-post … are careful."

    assert ai.stray_letter_splices(text) == []


def test_currency_and_maths_signs_are_not_splices():
    """From a live GSTBot row: "within a tolerance of ±₹5"."""
    text = f"{_BODY} ensuring values match within a tolerance of ±₹5 per invoice."

    assert ai.stray_letter_splices(text) == []


def test_a_middle_dot_separating_links_is_not_a_splice():
    """The footer every published N409 and Telechat post carries."""
    text = f"{_BODY} [n409.aiknol.com](https://n409.aiknol.com) · [GitHub](https://x.com)"

    assert ai.stray_letter_splices(text) == []


def test_text_written_in_another_language_is_not_contaminated_by_one():
    """Same escape hatch as the script gate: a translation is not a glitch."""
    german = "Bücher über Bücher, für größere Läden. " * 20

    assert ai.stray_letter_splices(german) == []


def test_a_run_the_script_gate_already_names_is_not_reported_twice():
    """One bad word should not read as two separate problems in the queue."""
    text = f"{_BODY} selects a publish time {CYRILLIC}."

    assert ai.stray_script_runs(text) == [CYRILLIC]
    assert ai.stray_letter_splices(text) == []


def test_an_unknown_accented_name_is_held_for_review():
    """The documented cost of the trade, pinned so it is a decision not a bug.

    "Zürich" is a real word and this holds it back. The gate cannot tell it from
    "stärker", which is also a real German word and was a sampler slip in a live
    GoSumo post — no property of the letters separates them. One glance at a
    review queue is the price of not publishing the other one.
    """
    assert ai.stray_letter_splices(f"{_BODY} our Zürich office opened.") == ["Zürich"]


# --------------------------------------------------------------------------- #
# The splice gate, through the pipeline                                        #
# --------------------------------------------------------------------------- #


def test_a_spliced_body_goes_to_review_instead_of_publishing(db, auto_project, writes):
    """The bug this half fixes: two of these published, and nothing looked."""
    writes(_generated(body_markdown=f"{_BODY} a nətive file picker uploads it."))

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW


def test_a_spliced_title_goes_to_review(db, auto_project, writes):
    writes(_generated(title="Retries in Herald, démontrated"))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_a_spliced_excerpt_goes_to_review(db, auto_project, writes):
    writes(_generated(excerpt="How the stärker dashboard shows retries."))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_a_spliced_meta_description_goes_to_review(db, auto_project, writes):
    writes(
        _generated(
            meta_description=(
                "How retries work in Herald, why a retry never double-posts, and "
                "what the tört backoff does when a platform is down."
            )
        )
    )

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_the_splices_reach_the_reviewer(db, auto_project, writes):
    writes(_generated(body_markdown=f"{_BODY} a nətive file picker uploads it."))

    routed = _route(db, auto_project)

    assert routed.garbled_runs == ["nətive"]
    assert routed.content.source["garbled_runs"] == ["nətive"]


def test_both_gates_report_into_the_same_list(db, auto_project, writes):
    """A piece carrying one of each names both, so the reviewer edits once."""
    writes(
        _generated(
            body_markdown=f"{_BODY} a publish time {CYRILLIC} and a nətive picker."
        )
    )

    routed = _route(db, auto_project)

    assert routed.garbled_runs == [CYRILLIC, "nətive"]


def test_a_clean_piece_with_a_loanword_still_auto_publishes(db, auto_project, writes):
    """The false positive that would matter most: it would hold back real posts."""
    writes(_generated(body_markdown=f"{_BODY} It collects résumés into a pipeline."))

    assert _route(db, auto_project).status == content_pipeline.AUTO_PUBLISHED
