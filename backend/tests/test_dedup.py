"""Direct tests for :mod:`app.services.dedup`.

The module was reachable only through its callers — the autopilot's scan, the
generator's idea backlog, the pipeline's post-generation check — so every
property of it was asserted at one remove, through a fixture that had to
produce a whole repo scan to ask a question about two strings.

Two things make it worth testing directly. The **threshold** is tuned against a
specific pair of headlines it must keep apart, and that pair is stated in the
source as a comment rather than as a test. And ``duplicate_of`` answers *two*
questions over one query — "are these the same commits" and "does this say the
same thing" — with a documented precedence between them that no caller
exercises both halves of.

Nothing here builds a scan. That is the point.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentType
from app.models.mixins import utcnow
from app.services import dedup


def _piece(
    db,
    project,
    *,
    title: str,
    shas: list[str] | None = None,
    age_days: int = 0,
    source: dict | None = None,
) -> Content:
    """One stored piece, with optional recorded provenance."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.FEATURE_SPOTLIGHT,
        title=title,
        slug=title.lower().replace(" ", "-")[:300] + f"-{utcnow().timestamp()}"[:19],
        source=source
        if source is not None
        else ({dedup.SOURCE_COMMITS_KEY: shas} if shas else None),
    )
    db.add(row)
    db.flush()
    if age_days:
        row.created_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# tokens                                                                       #
# --------------------------------------------------------------------------- #


def test_stopwords_and_possessives_fall_out():
    """Two phrasings of one idea reduce to the same words."""
    assert dedup.tokens("Pulse's caching layer") == dedup.tokens(
        "the caching layer in Pulse"
    )


def test_a_curly_apostrophe_is_the_same_as_a_straight_one():
    """The model emits them and a Mac produces them by autocorrect."""
    assert dedup.tokens("Pulse’s caching") == dedup.tokens("Pulse's caching")


@pytest.mark.parametrize("headline", ["2.0", "double-post"])
def test_a_version_number_stays_one_word(headline):
    """Split into "2" and "0", every release headline looks like every other."""
    assert dedup.tokens(headline) == frozenset({headline})


def test_case_is_not_a_difference():
    assert dedup.tokens("CACHING Layer") == dedup.tokens("caching layer")


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_empty_headline_has_no_tokens(value):
    """``None`` reaches this from a JSON column that may hold anything."""
    assert dedup.tokens(value) == frozenset()


# --------------------------------------------------------------------------- #
# is_restatement — the threshold                                               #
# --------------------------------------------------------------------------- #


def test_the_pair_the_threshold_was_tuned_to_keep_apart():
    """The comment on ``SIMILARITY_THRESHOLD``, as an assertion.

    "Getting started with Pulse" and "Getting started with Pulse Pro" share
    three significant words of four — 0.75 — and are two different pieces. This
    is the pair the 0.8 exists for, and a well-meant loosening to 0.7 makes
    Pulse stop writing about a new product because it already wrote about the
    old one.
    """
    existing = dedup.tokens("Getting started with Pulse")
    assert not dedup.is_restatement("Getting started with Pulse Pro", existing)


def test_a_rephrasing_is_caught():
    """What the check is actually for — the same piece, worded differently.

    The words that differ here are all stopwords, which is the shape the
    generator actually produces: the same brief comes back with the same nouns
    and a different sentence around them.
    """
    existing = dedup.tokens("How to deploy Pulse to production")
    assert dedup.is_restatement("Deploy Pulse in production", existing)


def test_a_reworded_verb_is_not_caught_because_this_is_not_a_stemmer():
    """A documented limitation, pinned so it is a decision rather than a bug.

    "deploy" and "deploying" are two tokens, so a headline that inflects its
    verb scores below the threshold and is written twice. The module says as
    much — its four lines of Jaccard are deliberately not a stemmer — and the
    cost of the miss is one duplicate parked in review, against the cost of
    stemming, which is "Getting started with Pulse" swallowing "Pulse Pro".

    If a stemmer is ever added, this test fails and should be deleted. That is
    the intent: the failure is the notification.
    """
    existing = dedup.tokens("How to deploy Pulse to production")
    assert not dedup.is_restatement("Deploying Pulse into production", existing)


def test_an_exact_repeat_is_a_restatement():
    existing = dedup.tokens("Caching in Pulse explained")
    assert dedup.is_restatement("Caching in Pulse explained", existing)


def test_two_unrelated_headlines_are_not():
    existing = dedup.tokens("Caching in Pulse explained")
    assert not dedup.is_restatement("Announcing the new calendar view", existing)


@pytest.mark.parametrize(
    "candidate,existing_title,expected",
    [
        # Under two significant words there is not enough subject to judge
        # overlap on, so it is exact match only down there.
        ("Caching", "Caching", True),
        ("Caching", "Webhooks", False),
        # One shared word would otherwise score 1.0 against another one-word
        # headline.
        ("Caching", "Caching layer performance", False),
    ],
)
def test_a_one_word_headline_is_compared_exactly(candidate, existing_title, expected):
    assert (
        dedup.is_restatement(candidate, dedup.tokens(existing_title)) is expected
    )


def test_nothing_is_a_restatement_of_nothing():
    """Both directions — an empty candidate and an empty stored title."""
    assert not dedup.is_restatement("", dedup.tokens("Caching in Pulse"))
    assert not dedup.is_restatement("Caching in Pulse", frozenset())


# --------------------------------------------------------------------------- #
# commit_shas                                                                  #
# --------------------------------------------------------------------------- #


class _Commit:
    def __init__(self, sha):
        self.sha = sha


class _Activity:
    def __init__(self, *shas):
        self.new_commits = [_Commit(s) for s in shas]


def test_shas_are_abbreviated_to_seven():
    """Seven is what a human reads and what git abbreviates to."""
    assert dedup.commit_shas(_Activity("a" * 40)) == ["a" * 7]


def test_shas_are_lowercased_and_stripped():
    assert dedup.commit_shas(_Activity("  ABC1234DEF  ")) == ["abc1234"]


def test_the_stored_list_is_capped():
    """A scan can arrive holding a full page of a hundred.

    The list goes into a JSON column every listing query and every editor
    render loads — the payload-width reasoning the model applies to bodies.
    """
    activity = _Activity(*[f"{i:07d}" for i in range(100)])
    assert len(dedup.commit_shas(activity)) == dedup.MAX_STORED_COMMITS


def test_the_newest_commits_are_the_ones_kept():
    """The list arrives newest-first, and recognising a repeat depends on it.

    Two scans of an overlapping window share their *newest* commits. Keeping
    the tail instead would make the second scan of a moved window look new.
    """
    activity = _Activity(*[f"{i:07d}" for i in range(30)])
    kept = dedup.commit_shas(activity)
    assert kept[0] == "0000000"
    assert len(kept) == dedup.MAX_STORED_COMMITS


def test_an_empty_sha_is_dropped_rather_than_stored():
    assert dedup.commit_shas(_Activity("", "  ", "abc1234")) == ["abc1234"]


@pytest.mark.parametrize("activity", [object(), None])
def test_something_carrying_no_commits_yields_nothing(activity):
    """Duck-typed on ``new_commits``: a signal and an activity both arrive."""
    assert dedup.commit_shas(activity) == []


# --------------------------------------------------------------------------- #
# duplicate_of — the two questions                                             #
# --------------------------------------------------------------------------- #


def test_a_repeated_commit_set_is_found(db, project):
    """The check that saves a generation — asked before the model is called."""
    stored = _piece(db, project, title="Anything at all", shas=["aaa1111", "bbb2222"])

    assert (
        dedup.duplicate_of(db, project.id, shas=["aaa1111", "bbb2222"]) == stored.id
    )


def test_a_subset_of_a_stored_commit_set_is_a_duplicate(db, project):
    """The case the autopilot actually produces.

    The watermark holds for an outage, the commits arrive again *enlarged*, and
    the second generation would write the first one's article. A scan holding
    commits already covered by a stored piece has nothing new to say.
    """
    stored = _piece(db, project, title="Anything", shas=["aaa1111", "bbb2222", "ccc3333"])

    assert dedup.duplicate_of(db, project.id, shas=["aaa1111"]) == stored.id


def test_a_commit_set_that_is_not_covered_is_not_a_duplicate(db, project):
    """One new commit among known ones is news."""
    _piece(db, project, title="Anything", shas=["aaa1111"])

    assert dedup.duplicate_of(db, project.id, shas=["aaa1111", "zzz9999"]) is None


def test_a_restated_title_is_found(db, project):
    """The check that saves the publish, asked after the model has answered."""
    stored = _piece(db, project, title="How to deploy Pulse to production")

    assert (
        dedup.duplicate_of(db, project.id, title="Deploy Pulse in production")
        == stored.id
    )


def test_commits_are_answered_before_titles(db, project):
    """The documented precedence: the exact answer wins.

    A piece that matches on shas matches whatever its title says, so the sha
    match must be returned even when a *different* stored piece would also
    match on the title.
    """
    by_shas = _piece(db, project, title="Completely unrelated wording", shas=["aaa1111"])
    _piece(db, project, title="Caching in Pulse explained")

    found = dedup.duplicate_of(
        db, project.id, title="Caching in Pulse explained", shas=["aaa1111"]
    )
    assert found == by_shas.id


def test_a_piece_outside_the_window_is_not_compared(db, project, monkeypatch):
    """Old enough and Pulse is allowed to write about it again."""
    monkeypatch.setattr(settings, "dedup_window_days", 30)
    _piece(db, project, title="Caching in Pulse explained", age_days=45)

    assert dedup.duplicate_of(db, project.id, title="Caching in Pulse explained") is None


def test_another_project_is_not_compared(db, project, user):
    """Two projects may legitimately publish the same headline."""
    from app.models.project import Project, Tone

    other = Project(
        user_id=user.id,
        name="Other",
        slug="other",
        repo_url="https://github.com/r2st/Other",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.commit()
    _piece(db, project, title="Caching explained properly")

    assert dedup.duplicate_of(db, other.id, title="Caching explained properly") is None


def test_an_archived_piece_still_counts_as_a_duplicate(db, project):
    """Deliberate, and the most annoying version of the bug if it were not.

    Archiving says "I do not want this", which is not the same as "write it
    again" — and the twin of something just withdrawn is what would arrive.
    """
    from app.models.content import ContentStatus

    stored = _piece(db, project, title="Caching in Pulse explained")
    stored.status = ContentStatus.ARCHIVED
    db.commit()

    assert (
        dedup.duplicate_of(db, project.id, title="Caching in Pulse explained")
        == stored.id
    )


def test_asking_nothing_finds_nothing(db, project):
    """No title and no shas is not a question, and must not match everything."""
    _piece(db, project, title="Caching in Pulse explained")

    assert dedup.duplicate_of(db, project.id) is None
    assert dedup.duplicate_of(db, project.id, title="   ") is None


# --------------------------------------------------------------------------- #
# Stored provenance is read defensively                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        # A piece written before the key existed.
        {"kind": "manual"},
        # A future version storing something else under it.
        {dedup.SOURCE_COMMITS_KEY: "aaa1111"},
        {dedup.SOURCE_COMMITS_KEY: {"sha": "aaa1111"}},
        # The seeder writes this column too.
        {dedup.SOURCE_COMMITS_KEY: None},
    ],
)
def test_a_source_column_holding_anything_else_does_not_raise(db, project, source):
    """``source`` is JSON written by several versions of this code."""
    _piece(db, project, title="Anything", source=source)

    assert dedup.duplicate_of(db, project.id, shas=["aaa1111"]) is None


def test_a_stored_full_length_sha_matches_an_abbreviated_one(db, project):
    """Stored shas are compared against stored shas, never against GitHub.

    A row written by an older version holding forty characters still has to
    match the seven-character form the current one produces.
    """
    stored = _piece(db, project, title="Anything", source={dedup.SOURCE_COMMITS_KEY: ["A" * 40]})

    assert dedup.duplicate_of(db, project.id, shas=["a" * 40]) == stored.id
