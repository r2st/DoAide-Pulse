"""Two scans of related commits must not bank the same idea twice.

Nothing deduplicated ``ContentIdea`` rows, and both producers repeat by
construction:

* the autopilot scans on a schedule and asks a model at ``temperature=0.9``
  about an overlapping window of commits, so consecutive scans of an active repo
  describe the same work in slightly different words;
* when no provider answers, ``_fallback_ideas`` returns a *fixed* string — every
  scan during an outage banked another "What's new in ``<project>``", hourly;
* and ``GET /projects/{id}/ideas?refresh=true`` is a button, pressed again by
  the user who did not like the list.

The damage is not clutter. ``_prune_ideas`` bounds the table by deleting the
*oldest* unused rows, so a repeating producer does not fill the list and stop —
it evicts the varied ideas banked before it, one per repeat, until the
suggestions are N copies of one headline. The failure runs toward less choice
the longer it runs.

These tests pin what counts as a restatement, what deliberately does not, and
that the first row of a group keeps its ``created_at`` — refreshing it on each
repeat would make a duplicated idea permanently unprunable, which is the same
bug with the sign flipped.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentIdea, ContentType
from app.services import content_generator
from app.services.content_generator import Idea


def _bank(db, project, *headlines, source=None) -> list[ContentIdea]:
    return content_generator.bank_ideas(
        db,
        project.id,
        [Idea(ContentType.FEATURE_SPOTLIGHT, h, "because") for h in headlines],
        source=source or {"kind": "test"},
    )


def _headlines(db, project) -> list[str]:
    return sorted(
        row.headline
        for row in db.query(ContentIdea).filter(ContentIdea.project_id == project.id)
    )


# --------------------------------------------------------------------------- #
# What counts as a restatement                                                 #
# --------------------------------------------------------------------------- #


def test_the_same_headline_twice_is_banked_once(db, project):
    """The LLM-outage case, which is exact repetition by the hour."""
    _bank(db, project, "What's new in Pulse")
    banked = _bank(db, project, "What's new in Pulse")
    db.commit()

    assert banked == []
    assert _headlines(db, project) == ["What's new in Pulse"]


def test_a_repeated_headline_inside_one_batch_is_banked_once(db, project):
    """A model asked for four ideas can return the same one twice, and does."""
    banked = _bank(db, project, "Retry logic that never double-posts", "Retry "
                   "logic that never double-posts")
    db.commit()

    assert len(banked) == 1


def test_punctuation_and_case_do_not_make_a_new_idea(db, project):
    _bank(db, project, "What's New In Pulse!")
    _bank(db, project, "what's new in pulse")
    db.commit()

    assert len(_headlines(db, project)) == 1


def test_a_possessive_is_not_a_different_subject(db, project):
    """"Pulse's caching layer" and "the caching layer in Pulse" are one piece."""
    _bank(db, project, "Pulse's caching layer")
    _bank(db, project, "The caching layer in Pulse")
    db.commit()

    assert len(_headlines(db, project)) == 1


def test_a_version_number_is_not_split_into_digits(db, project):
    """Guarding the tokenizer, not the threshold.

    Split on the dot, "2.0" and "3.0" both contribute a "0" and the two release
    announcements start to look like the same idea.
    """
    assert content_generator._idea_tokens("Pulse 2.0 is out") == frozenset(
        {"pulse", "2.0", "out"}
    )


def test_a_headline_with_no_significant_words_is_nobody_else_s_restatement(db, project):
    """Nothing to compare is not the same as comparing equal.

    ``_idea_tokens`` drops stopwords, so a headline that is entirely stopwords —
    or an empty one, which is what a model returns on a bad afternoon — comes
    back as an empty set. An overlap ratio over two empty sets is either a
    division by zero or a vacuous 1.0, and 1.0 would mean every blank headline
    swallowed the next real one. The early return is the guard; both directions
    of it are checked because ``existing`` is empty exactly as often.
    """
    real = content_generator._idea_tokens("Shipping the calendar")

    assert content_generator._is_restatement("of the and", real) is False
    assert content_generator._is_restatement("", real) is False
    assert content_generator._is_restatement("Shipping the calendar", frozenset()) is False


def test_a_rephrasing_with_different_filler_words_is_the_same_idea(db, project):
    """"to production" against "into production" is one subject, twice.

    This is what the scan-over-overlapping-commits case actually looks like: the
    model is not deterministic, so the second telling is never byte-identical.
    """
    _bank(db, project, "How to deploy Pulse to production")
    _bank(db, project, "How to deploy Pulse into production")
    db.commit()

    assert len(_headlines(db, project)) == 1


def test_word_order_does_not_make_a_new_idea(db, project):
    _bank(db, project, "Caching and retries in Pulse")
    _bank(db, project, "Retries and caching in Pulse")
    db.commit()

    assert len(_headlines(db, project)) == 1


# --------------------------------------------------------------------------- #
# What deliberately is not                                                     #
# --------------------------------------------------------------------------- #


def test_two_genuinely_different_ideas_are_both_kept(db, project):
    _bank(db, project, "How retries work in Pulse")
    _bank(db, project, "Scheduling posts across five platforms")
    db.commit()

    assert len(_headlines(db, project)) == 2


def test_one_extra_significant_word_is_a_different_piece(db, project):
    """The pair `IDEA_SIMILARITY_THRESHOLD` is tuned against.

    "Getting started with Pulse" and "Getting started with Pulse Pro" share
    three significant words of four — 0.75 — and are two different articles. A
    looser threshold merges them and the second never gets written.
    """
    _bank(db, project, "Getting started with Pulse")
    _bank(db, project, "Getting started with Pulse Pro")
    db.commit()

    assert len(_headlines(db, project)) == 2


def test_two_releases_are_two_announcements(db, project):
    """`_fallback_ideas` builds these from the tag, and the tags differ."""
    _bank(db, project, "Pulse 2.0 is out")
    _bank(db, project, "Pulse 3.0 is out")
    db.commit()

    assert len(_headlines(db, project)) == 2


def test_a_one_word_headline_does_not_swallow_a_longer_one(db, project):
    """Under two significant words there is no subject to judge overlap on.

    A single shared word would otherwise score 1.0 against any other one-word
    headline, so down there the comparison is exact-match only.
    """
    _bank(db, project, "Caching")
    _bank(db, project, "Retries")
    db.commit()

    assert len(_headlines(db, project)) == 2


def test_an_idea_already_written_up_does_not_block_a_new_one(db, project):
    """Comparison is against *unused* ideas only.

    An idea with a draft against it is a subject the project has covered.
    Whether to cover it again is an editorial judgement about repetition, and
    not one a headline comparison should make on its own.
    """
    used = ContentIdea(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        headline="What's new in Pulse",
        rationale="",
        source={},
        used_content_id=4242,
    )
    db.add(used)
    db.commit()

    banked = _bank(db, project, "What's new in Pulse")
    db.commit()

    assert len(banked) == 1


def test_another_projects_ideas_do_not_block_this_ones(db, project, user):
    from app.models.project import Project, Tone

    other = Project(
        user_id=user.id,
        name="Other",
        slug="other",
        description="Different repo, same house style.",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.commit()
    _bank(db, other, "What's new in Pulse")

    banked = _bank(db, project, "What's new in Pulse")
    db.commit()

    assert len(banked) == 1


# --------------------------------------------------------------------------- #
# The first row wins, and keeps its age                                        #
# --------------------------------------------------------------------------- #


def test_the_surviving_row_keeps_its_original_rationale_and_source(db, project):
    """The first telling stands; the repeat is dropped, not merged over it."""
    _bank(db, project, "What's new in Pulse", source={"kind": "autopilot"})
    db.commit()
    _bank(db, project, "What's new in Pulse", source={"kind": "manual_refresh"})
    db.commit()

    row = db.query(ContentIdea).filter(ContentIdea.project_id == project.id).one()
    assert row.source == {"kind": "autopilot"}


def test_repeating_does_not_keep_an_idea_permanently_unprunable(db, project):
    """``_prune_ideas`` evicts by ``created_at``, so touching it on each repeat
    would make the most-repeated idea the last one ever deleted — the same bug
    upside down.
    """
    _bank(db, project, "What's new in Pulse")
    db.commit()
    row = db.query(ContentIdea).filter(ContentIdea.project_id == project.id).one()
    first_seen = row.created_at

    _bank(db, project, "What's new in Pulse")
    db.commit()
    db.refresh(row)

    assert row.created_at == first_seen


# --------------------------------------------------------------------------- #
# Through the producers                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def one_idea(monkeypatch):
    """Every call to the generator returns the same idea — the outage case."""
    monkeypatch.setattr(
        content_generator,
        "suggest_ideas",
        lambda project, **kw: [
            Idea(ContentType.FEATURE_SPOTLIGHT, f"What's new in {project.name}", "n commits")
        ],
    )


def test_refreshing_the_ideas_list_repeatedly_does_not_fill_it_with_one_headline(
    client, auth, db, project, one_idea
):
    """Three presses of a button that asks a model the same question."""
    for _ in range(3):
        resp = client.get(
            f"/api/v1/projects/{project.id}/ideas?refresh=true", headers=auth
        )
        assert resp.status_code == 200, resp.text

    assert len(resp.json()) == 1


def test_repeated_scans_do_not_evict_the_varied_ideas_they_sit_next_to(
    db, project, one_idea
):
    """The eviction, which is what makes this worse than clutter.

    Ten scans during an LLM outage against a cap of five: without dedupe the
    five real ideas below are gone and the list is five copies of one sentence.
    """
    from app.tasks import autopilot_tasks

    for n in range(5):
        _bank(db, project, f"Genuinely distinct subject number {n} about scheduling")
    db.commit()

    for _ in range(10):
        content_generator.bank_ideas(
            db,
            project.id,
            content_generator.suggest_ideas(project),
            source={"kind": "autopilot"},
        )
        autopilot_tasks._prune_ideas(db, project.id)
    db.commit()

    surviving = _headlines(db, project)
    assert sum(1 for h in surviving if h.startswith("Genuinely distinct")) == 5
    assert sum(1 for h in surviving if h.startswith("What's new")) == 1
