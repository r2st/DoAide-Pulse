"""Readability, code density, and the floor on DRAFT → REVIEW.

Two things are under test here and they are deliberately in one file, because
the second is only as good as the first: the gate refuses a piece on a number,
and a number that does not track what a reader would say about the piece is a
gate that refuses the wrong pieces.

So the measurement tests pin *relations* rather than constants wherever they
can — this body reads more easily than that one, this one is more code than
that one — since the exact Flesch output is arithmetic anyone can re-derive and
pinning it to a decimal would make every future tweak to the sentence splitter
look like a regression. The two absolute assertions are the ones that carry
consequences: an empty body scores zero, and a code dump scores under the
shipped floor.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.services import quality

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

PLAIN = (
    "Pulse reads your commits. It writes a post about them. A human reads the "
    "post before it goes out.\n\n"
    "The post lands in the review queue. You approve it or you do not. Nothing "
    "is published behind your back.\n"
)

DENSE = (
    "Notwithstanding the aforementioned considerations regarding the "
    "instantiation of the publication subsystem, the architectural "
    "determination to encapsulate the heterogeneous platform adaptors behind a "
    "singular abstraction necessitated a corresponding reconsideration of the "
    "credential materialisation strategy, particularly insofar as the "
    "encryption boundary was concerned, which had previously been "
    "characterised by an unfortunate diffusion of responsibilities across "
    "multiple collaborating modules whose individual justifications were "
    "defensible but whose aggregate consequence was an obscurity of intent.\n"
)

CODE_DUMP = (
    "Run it.\n\n"
    "```python\n"
    + "publication.duration_ms = elapsed_ms(started)\n" * 30
    + "```\n"
)


def _piece(db, project, *, body: str, title: str = "A piece", status=ContentStatus.DRAFT):
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{utcnow().timestamp()}",
        body_markdown=body,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Flesch-Kincaid                                                               #
# --------------------------------------------------------------------------- #


def test_plain_prose_reads_more_easily_than_dense_prose():
    """The whole claim the readability half of the score rests on."""
    plain = quality.readability(PLAIN)
    dense = quality.readability(DENSE)

    assert plain.reading_ease > dense.reading_ease
    assert plain.grade_level < dense.grade_level


def test_the_ease_scale_does_not_run_off_either_end():
    """Both formulas are unbounded; a score mixed into another score cannot be.

    The dense paragraph above is one 90-word sentence of polysyllables, which
    is exactly the input that sends the raw formula negative.
    """
    assert 0.0 <= quality.readability(DENSE).reading_ease <= 100.0
    assert quality.readability(DENSE).grade_level >= 0.0


def test_a_body_too_short_to_measure_says_so_rather_than_scoring_zero():
    """A tweet is not badly written. It is unmeasurable, which is different.

    Pulse writes social posts from the same pipeline as articles, so if the
    absence of a reading ease were scored as a bad one, every tweet in the
    install would sit below any threshold worth setting.
    """
    measured = quality.readability("Pulse 1.0 is out today.")

    assert measured.words < quality.MIN_WORDS_FOR_READABILITY
    assert measured.reading_ease is None
    assert measured.grade_level is None
    assert quality.readability_points(None) is None


def test_the_missing_readability_weight_goes_to_the_code_component():
    """Not back into the pool, which would hand most of it to the SEO score.

    That was the bug: a body of thirty fenced lines and the words "Run it." has
    no measurable prose, and the obvious renormalisation gave 71% of its total
    to the one measure a thin piece is *good* at. It scored 50 against a floor
    of 50. A social post is unaffected either way — it takes full marks for
    code density — so the redistribution only bites where the missing prose and
    the code are the same fact.
    """
    short = quality.report(
        title="Pulse 1.0",
        body_markdown="Pulse 1.0 is out today. It writes your changelog.",
        meta_description="Pulse 1.0 is out today, and it writes your changelog for you.",
        keywords=["pulse"],
        focus_keyword="pulse",
        slug="pulse-1-0",
    )

    assert short.readability_points is None
    assert short.code_points == 100
    expected = quality.SEO_WEIGHT * short.seo_score + (
        quality.CODE_WEIGHT + quality.READABILITY_WEIGHT
    ) * short.code_points
    assert short.score == round(expected)


def test_headings_and_bullets_are_sentences_rather_than_one_long_one():
    """The reason this module does not reuse ``seo.strip_markdown``.

    Collapsed to a single line, a list under a heading is one enormous
    sentence, and a perfectly readable post is reported as unreadable because
    of how it is formatted.
    """
    listed = "## What changed\n\n- Latency is recorded\n- Slow queries are logged\n"

    assert quality.readability(listed).sentences == 3


def test_a_word_always_has_a_syllable():
    """Both formulas divide by the word count and multiply by this one."""
    assert quality.syllables("code") == 1
    assert quality.syllables("table") == 2
    assert quality.syllables("see") == 1
    assert quality.syllables("2026") == 1


# --------------------------------------------------------------------------- #
# Code-to-text                                                                 #
# --------------------------------------------------------------------------- #


def test_a_paste_is_mostly_code_and_a_post_is_not():
    assert quality.code_ratio(CODE_DUMP) > 0.8
    assert quality.code_ratio(PLAIN) == 0.0


def test_an_unterminated_fence_still_counts_as_code():
    """The one case a naive ``` ... ``` match scores at zero.

    A model that forgets the closing fence has almost always done so because it
    was still emitting code when it ran out — so the body most likely to be all
    code was the body the ratio reported as all prose.
    """
    assert quality.code_ratio("Here you go:\n\n```python\nx = 1\ny = 2\n") > 0.5


def test_inline_code_is_counted_once_and_not_twice():
    """Backticks inside a fence are part of the fence, not a second span."""
    fenced_only = quality.code_ratio("Text.\n\n```\na = `b`\n```\n")
    assert fenced_only <= 1.0


def test_an_indented_list_is_not_a_code_block():
    """Four leading spaces is also how a nested bullet continues.

    Counting indentation as code would report an ordinary bulleted post as a
    paste, which is the false positive that would make the gate a nuisance.
    """
    nested = "- One\n    - Under one\n- Two\n    - Under two\n"
    assert quality.code_ratio(nested) == 0.0


# --------------------------------------------------------------------------- #
# The combined score                                                           #
# --------------------------------------------------------------------------- #


def test_an_ordinary_post_clears_the_shipped_floor():
    """The floor has to let through the thing Pulse writes all day."""
    report = quality.report(
        title="Platform latency, recorded",
        body_markdown=PLAIN,
        meta_description=(
            "Pulse now records how long each platform API call takes, so a "
            "slow destination shows up in the metrics."
        ),
        keywords=["latency", "metrics"],
        focus_keyword="latency",
        slug="platform-latency-recorded",
    )
    assert report.score >= settings.content_quality_min_score


def test_a_code_dump_does_not():
    """And the reason it does not is the ratio, not the envelope.

    Asserted together on purpose: a code dump with a good title, description
    and keyword scores *well* on every SEO check there is, so if the total
    still passed, the code component would be decorative.
    """
    report = quality.report(
        title="Recording platform latency",
        body_markdown=CODE_DUMP,
        meta_description=(
            "Recording platform latency on the publication row, with the "
            "thirty lines it took to do it."
        ),
        keywords=["latency"],
        focus_keyword="latency",
        slug="recording-platform-latency",
    )

    assert report.seo_score > 50
    assert report.code_points < 30
    assert report.score < settings.content_quality_min_score


def test_an_empty_body_scores_nothing_at_all():
    """Neither generous component can see that there is nothing there.

    No reading ease to fail, full marks for having no code, and five points off
    the SEO score for the word count — which came to 61 out of 100 for the
    empty string.
    """
    assert quality.report(
        title="A piece", body_markdown="", meta_description="", keywords=[], slug="p"
    ).score == 0


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #


def test_a_thin_draft_cannot_ask_for_a_reader(client, auth, db, project):
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 409, resp.text
    db.refresh(piece)
    assert piece.status == ContentStatus.DRAFT


def test_the_refusal_says_which_number_was_low(client, auth, db, project):
    """A verdict with no components is a rule the writer cannot act on."""
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    detail = resp.json()["detail"]
    assert str(settings.content_quality_min_score) in detail
    assert "% of the body is code" in detail


def test_a_good_draft_goes_through(client, auth, db, project):
    piece = _piece(db, project, body=PLAIN)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"


def test_the_patch_door_applies_the_same_floor(client, auth, db, project):
    """Two doors to one column. A rule on one of them is not a rule."""
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.patch(
        f"/api/v1/content/{piece.id}", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 409, resp.text


def test_fixing_the_body_and_submitting_it_is_one_request(client, auth, db, project):
    """The piece is scored as it will be, not as it was.

    Otherwise the only way out of the refusal is two calls in a fixed order,
    which is a workflow the error message does not describe and nobody would
    guess.
    """
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        headers=auth,
        json={"body_markdown": PLAIN, "status": "review"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"


def test_a_refused_submission_leaves_the_edit_behind(client, auth, db, project):
    """Nothing is committed on the way out of the gate.

    A PATCH that changed the body *and* failed the gate must not leave the new
    body on the row: the caller was told the request did not happen, and a
    half-applied edit is worse than either answer.
    """
    piece = _piece(db, project, body=PLAIN)
    original = piece.body_markdown

    resp = client.patch(
        f"/api/v1/content/{piece.id}",
        headers=auth,
        json={"body_markdown": CODE_DUMP, "status": "review"},
    )

    assert resp.status_code == 409, resp.text
    db.expire_all()
    db.refresh(piece)
    assert piece.body_markdown == original
    assert piece.status == ContentStatus.DRAFT


@pytest.mark.parametrize("target", ["approved", "archived"])
def test_the_floor_is_only_on_asking_for_a_reader(client, auth, db, project, target):
    """Approving is a human saying yes; archiving is saying it is not going out.

    A gate on either would be a gate overruling the reviewer, and on archive it
    would trap the worst pieces in the queue permanently — which is the exact
    opposite of what a quality floor is for.
    """
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": target}
    )

    assert resp.status_code == 200, resp.text


def test_a_piece_already_in_review_is_not_re_judged(client, auth, db, project):
    """The floor is on the transition, not on the state.

    A piece the autopilot routed straight to review has been through five gates
    and is in front of a human *because* one of them fired. Re-judging it here
    would leave it nowhere to go but the bin.
    """
    piece = _piece(db, project, body=CODE_DUMP, status=ContentStatus.REVIEW)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 200, resp.text


def test_the_gate_can_be_switched_off(client, auth, db, project, monkeypatch):
    monkeypatch.setattr(settings, "content_quality_gate_enabled", False)
    piece = _piece(db, project, body=CODE_DUMP)

    resp = client.post(
        f"/api/v1/content/{piece.id}/status", headers=auth, json={"status": "review"}
    )

    assert resp.status_code == 200, resp.text


def test_the_editor_is_shown_the_score_the_gate_will_apply(client, auth, db, project):
    """A rule the writer can only discover by hitting it is not a rule."""
    piece = _piece(db, project, body=CODE_DUMP)

    detail = client.get(f"/api/v1/content/{piece.id}", headers=auth).json()

    assert detail["quality"]["score"] == quality.report_for(piece).score
    assert detail["quality"]["code_ratio"] > 0.8
