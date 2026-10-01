"""Tags as a vocabulary rather than a pile of strings.

``Content.tags`` is a JSON list, and a JSON list has no opinion about whether
``release``, ``releases``, ``Release`` and ``product-release`` are four tags or
one. This file is about the rules that give it one: a canonical form, a
separator that means containment, and a rename that moves a subtree.

Everything here is pure — no session, no rows. The endpoints are next door in
``test_a_tag_rename_moves_the_subtree_with_it``.
"""
from __future__ import annotations

import pytest

from app.models.content import TAG_MAX_LENGTH
from app.services import tags

# --------------------------------------------------------------------------- #
# The canonical form                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Release Notes", "release-notes"),
        ("  spaced  ", "spaced"),
        ("Guides / Deployment", "guides/deployment"),
        ("guides//docker", "guides/docker"),
        ("/guides/", "guides"),
        ("C#", "c"),
        ("node.js", "node-js"),
        ("---dashes---", "dashes"),
        ("MiXeD/CaSe", "mixed/case"),
    ],
)
def test_a_tag_has_one_spelling(raw, expected):
    """Case, punctuation and stray separators are noise, not intent.

    Empty segments are dropped rather than refused because they are almost
    always punctuation: ``"Guides / Deployment"`` has a space either side of the
    separator and ``"guides//docker"`` is a typo, and both name the two-level
    tag a person would draw.
    """
    assert tags.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "///", "?"])
def test_a_string_with_no_word_in_it_is_not_a_tag(raw):
    """The one shape that survives as nothing. Callers check for it."""
    assert tags.normalize(raw) == ""


def test_depth_is_capped_by_dropping_the_deepest_levels():
    """``a/b/c/d`` is not a facet anybody clicks through.

    The cap is a separate rule from the length one because they are separate
    mistakes: ``a/b/c/d/e/f`` is eleven characters and still unusable.
    """
    assert tags.normalize("a/b/c/d/e") == "a/b/c"
    assert len(tags.segments(tags.normalize("a/b/c/d/e"))) == tags.MAX_DEPTH


def test_a_long_segment_is_clipped_rather_than_dropped():
    """A tag with a runaway word in it is still about something.

    Contrast ``seo.normalize_keywords``, which drops an over-long keyword
    outright — half a keyword is no more searchable than all of it, but half a
    tag still files the piece under something.
    """
    tag = tags.normalize("x" * 200)
    assert tag == "x" * tags.SEGMENT_MAX_LENGTH


def test_the_canonical_form_always_fits_the_column():
    """``Content.tags`` is JSON and will not refuse an over-long entry itself,
    which is why the cap has to hold on this side of it."""
    monster = "/".join("y" * 300 for _ in range(8))
    assert len(tags.normalize(monster)) <= TAG_MAX_LENGTH


def test_a_list_keeps_its_order_and_loses_its_repeats():
    """Order matters: the first tag is the one a three-tag platform gets.

    Repeats are dropped after normalising, which is the point — ``Docker`` and
    ``docker`` are one tag arriving twice.
    """
    assert tags.normalize_all(["Docker", "docker", "K8s", "", "!!!", "Docker"]) == [
        "docker",
        "k8s",
    ]


def test_a_list_is_bounded():
    """A JSON column has no width of its own; the count cap is the schema's and
    this is the same cap applied where the service writes."""
    assert len(tags.normalize_all([f"tag{n}" for n in range(500)])) == 30
    assert len(tags.normalize_all([f"tag{n}" for n in range(500)], limit=4)) == 4


# --------------------------------------------------------------------------- #
# The separator means containment                                              #
# --------------------------------------------------------------------------- #


def test_a_tag_is_filed_under_every_level_above_it():
    assert tags.ancestors("guides/deployment/docker") == ["guides", "guides/deployment"]
    assert tags.ancestors("guides") == []


def test_expanding_a_list_adds_the_parents_a_filter_needs():
    """Asking for ``guides`` has to find the piece tagged ``guides/deployment``,
    and the piece's stored list does not contain the word ``guides``."""
    assert tags.expand(["guides/deployment/docker", "news"]) == [
        "guides",
        "guides/deployment",
        "guides/deployment/docker",
        "news",
    ]


def test_expanding_ignores_the_empty_string():
    assert tags.expand(["", "news"]) == ["news"]


def test_a_prefix_is_not_a_parent():
    """The bug a naive ``startswith`` produces, and the reason the separator has
    to be the next character: ``guides-advanced`` is a different tag."""
    assert tags.is_within("guides/docker", "guides")
    assert tags.is_within("guides", "guides")
    assert not tags.is_within("guides-advanced", "guides")
    assert not tags.is_within("guides", "guides/docker")


def test_the_leaf_is_what_a_platform_gets():
    """The parents are Pulse's filing system. On Dev.to the tag list is the
    reader's only navigation, and ``guidesdeploymentdocker`` — which is what
    ``normalize_tags`` would make of the path — is not a tag anybody follows."""
    assert tags.leaf("guides/deployment/docker") == "docker"
    assert tags.leaf("news") == "news"
    assert tags.leaf("") == ""


# --------------------------------------------------------------------------- #
# Renaming moves the subtree                                                   #
# --------------------------------------------------------------------------- #


def test_renaming_a_parent_re_parents_its_children():
    """The only reading of "rename a tag" that leaves the tree looking like the
    one the user was pointing at."""
    assert tags.rename_in(
        ["guides/docker", "guides", "news"], "guides", "howto"
    ) == ["howto/docker", "howto", "news"]


def test_renaming_leaves_a_lookalike_alone():
    assert tags.rename_in(["guides-advanced"], "guides", "howto") == [
        "guides-advanced"
    ]


def test_renaming_onto_an_existing_tag_is_a_merge():
    """No special case needed — the result is deduped, so a piece that carried
    both ends up carrying it once. It is called out in the response because it
    is the one outcome of a rename that loses information."""
    assert tags.rename_in(["guides", "howto"], "guides", "howto") == ["howto"]


def test_a_merge_can_happen_between_two_children():
    """Renaming ``guides`` onto ``howto`` collapses ``guides/x`` into an
    existing ``howto/x`` without ``howto`` itself appearing anywhere — which is
    why the endpoint counts merges from the list lengths rather than by looking
    for ``new`` in the original."""
    assert tags.rename_in(["guides/x", "howto/x"], "guides", "howto") == ["howto/x"]


def test_a_rename_that_would_be_too_deep_is_truncated_not_refused():
    """A write that fails is worse than a tag one level shallower than asked
    for: the rename is over a hundred rows and there is no partial to resume."""
    assert tags.rename_in(["a/b"], "a", "x/y/z") == ["x/y/z"]


def test_a_rename_normalises_what_it_writes_back():
    """The rows it touches come out canonical, which is the incidental tidying
    a rename is allowed to do — it is already rewriting the list."""
    assert tags.rename_in(["Guides/Docker", "News"], "guides", "howto") == [
        "howto/docker",
        "news",
    ]


# --------------------------------------------------------------------------- #
# The tree                                                                     #
# --------------------------------------------------------------------------- #


def test_a_parent_nothing_is_directly_tagged_with_still_appears():
    """An account whose only tag is ``guides/deployment`` has a ``guides`` node.

    Leaving it out would draw a forest of orphans and make the hierarchy
    invisible in exactly the account that has just started using one.
    """
    tree = tags.build_tree([["guides/deployment"]])
    assert [node.tag for node in tree.roots] == ["guides"]
    root = tree.roots[0]
    assert root.direct == 0
    assert root.total == 1
    assert [child.tag for child in root.children] == ["guides/deployment"]


def test_a_piece_tagged_with_both_a_parent_and_a_child_counts_once():
    """``expand`` runs per piece, not per tag. Counting per tag would make this
    piece count twice against ``guides`` and overstate every parent in the
    tree."""
    tree = tags.build_tree([["guides", "guides/docker"]])
    root = tree.roots[0]
    assert root.total == 1
    assert root.direct == 1
    assert root.children[0].total == 1


def test_direct_and_total_are_different_questions():
    lists = [["guides"], ["guides/docker"], ["guides/docker"], ["news"]]
    tree = tags.build_tree(lists)
    guides = next(n for n in tree.roots if n.tag == "guides")
    assert (guides.direct, guides.total) == (1, 3)
    docker = guides.children[0]
    assert (docker.direct, docker.total) == (2, 2)


def test_the_tree_is_ordered_busiest_first_with_alphabetical_ties():
    """The order a facet list wants, and stable — which matters because it is
    what a client renders and what these tests compare."""
    tree = tags.build_tree([["b"], ["b"], ["a"], ["c"]])
    assert [node.tag for node in tree.roots] == ["b", "a", "c"]


def test_the_tree_normalises_as_it_counts():
    """This is what makes the feature work on the rows that already exist.

    Nothing rewrote the stored lists, so ``Release``, ``release`` and
    ``RELEASE`` are three strings in the column and one tag in the tree.
    """
    tree = tags.build_tree([["Release"], ["release"], ["RELEASE"]])
    assert [node.tag for node in tree.roots] == ["release"]
    assert tree.roots[0].total == 3
    assert tree.distinct == 1


def test_an_empty_account_has_an_empty_tree():
    tree = tags.build_tree([[], []])
    assert tree.roots == []
    assert tree.distinct == 0


def test_a_node_knows_its_own_depth():
    tree = tags.build_tree([["a/b/c"]])
    node = tree.roots[0].children[0].children[0]
    assert node.tag == "a/b/c"
    assert node.depth == 3
    assert node.as_dict()["label"] == "c"
