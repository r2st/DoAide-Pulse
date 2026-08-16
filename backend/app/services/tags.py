"""Tags, and the shape they have when there are more than a dozen of them.

``Content.tags`` has always been a JSON list of short strings written by the
model and edited by hand. That is enough for one project and stops being enough
at about the point a person has three: ``release``, ``releases``, ``Release``
and ``product-release`` all exist, all mean the same thing, and nothing in
Herald could see that they did — the column has no vocabulary, only values.

This module adds the two things that turn a pile of strings into a taxonomy,
and deliberately does not add a table.

**A path, not a name.** A tag may be written ``guides/deployment/docker``. The
separator is the whole hierarchy: ``guides`` is a tag in its own right, every
piece tagged ``guides/deployment`` is also, for the purposes of counting and
filtering, tagged ``guides`` (see :func:`expand`), and a tree can be drawn from
the strings alone. Storing it as one string rather than a parent pointer is what
keeps this a change to a JSON column instead of a migration, a join, and a
second source of truth about which tags exist — and the platforms take a flat
string either way, so a hierarchy Herald cannot flatten on the way out is a
hierarchy that breaks publishing.

**Suggestion from the account's own vocabulary.** :func:`suggest` reads the
piece, but it ranks what it finds against the tags this user already uses. A
suggester that invents ``kubernetes-deployment-guide`` for a person whose eleven
other posts are all under ``guides/`` has made the pile worse; one that says
``guides/deployment`` has made it smaller. That is the difference between
auto-tagging and tag *management*, and it is why this takes a session.

Nothing here writes. Normalisation happens on the way into the column via
:mod:`app.schemas.content`, and the two endpoints that change stored tags do
their own writing — this module answers questions.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import TAG_MAX_LENGTH, Content
from app.models.project import Project
from app.services import dedup, seo

#: What separates a tag from its parent.
#:
#: ``/`` rather than ``:`` or ``.`` because it is the separator every reader
#: already parses as containment, and because it is one of the characters
#: ``publishers.formatting.normalize_tags`` strips — so a hierarchical tag
#: flattens to ``guidesdeploymentdocker`` on the way to a platform unless
#: something takes the leaf first. :func:`leaf` is that something.
SEPARATOR = "/"

#: How many levels a tag may have.
#:
#: Three is a taxonomy; six is a filesystem somebody is using as a database.
#: The cap is here rather than left to :data:`TAG_MAX_LENGTH` because depth and
#: length are different mistakes: ``a/b/c/d/e/f/g/h`` is 15 characters and
#: unusable as a facet, and the reason to refuse it is that nobody will ever
#: click through eight levels, not that it is long.
MAX_DEPTH = 3

#: The longest one *segment* may be. Four segments of this plus separators sit
#: just inside :data:`TAG_MAX_LENGTH`, which is the real ceiling — the column's.
SEGMENT_MAX_LENGTH = 24

#: Characters kept in a segment. Everything else becomes a hyphen, and runs of
#: hyphens collapse: ``"Release Notes!"`` is ``release-notes``.
#:
#: Hyphens rather than deletion, unlike ``normalize_tags``, which has to satisfy
#: Dev.to's alphanumeric-only rule and so turns ``release notes`` into
#: ``releasenotes``. That rule belongs to the wire format, not to Herald's own
#: vocabulary: two words run together are unreadable in a facet list, and the
#: adapter still gets its stripped form because it still calls its own
#: normaliser on whatever is stored.
_UNSAFE = re.compile(r"[^a-z0-9]+")

#: How many pieces the tree walks before it stops and says so.
#:
#: The scan reads one narrow column and no bodies (see :func:`tree`), so this is
#: generous — but it is not absent, because "every piece this account has ever
#: written" is not a bound, and an account that has been open for three years is
#: exactly the one that wants a tag tree.
TREE_SCAN_LIMIT = 5000

#: The most tags :func:`suggest` will propose.
SUGGESTION_LIMIT = 8

#: How many times a word must appear in a piece before it is worth proposing as
#: a tag on its own evidence. One mention is a passing reference; the tag list
#: is for what the piece is *about*.
MIN_TERM_FREQUENCY = 3

#: One word of a body, for counting.
#:
#: The same shape as :mod:`app.services.dedup`'s and not imported from it: that
#: one is private, and the two want different things from a near-identical
#: pattern. ``dedup`` compares two headlines and wants ``2.0`` and
#: ``double-post`` held together; this counts body words and wants the same,
#: for the same reason — ``fastapi`` and ``fast`` are not the same tag. What is
#: shared is the judgement that matters, :data:`app.services.dedup.STOPWORDS`,
#: which is public and imported below.
_WORD = re.compile(r"[0-9a-z]+(?:[.\-][0-9a-z]+)*")


def normalize(raw: str) -> str:
    """One tag in its canonical form, or ``""`` if nothing survives.

    Lowercased, split on :data:`SEPARATOR`, each segment reduced to
    ``[a-z0-9-]`` and clipped to :data:`SEGMENT_MAX_LENGTH`, empty segments
    dropped, and the whole thing cut to :data:`MAX_DEPTH` levels.

    Empty segments are dropped rather than refused because they are almost
    always punctuation rather than intent: ``"Guides / Deployment"`` has a space
    on either side of the separator and ``"guides//docker"`` is a typo, and both
    mean the two-level tag a person would draw. The one shape that returns
    ``""`` is a tag with no alphanumeric character anywhere in it, which is not
    a tag under any reading.
    """
    parts: list[str] = []
    for segment in str(raw).lower().split(SEPARATOR):
        cleaned = _UNSAFE.sub("-", segment).strip("-")
        if cleaned:
            parts.append(cleaned[:SEGMENT_MAX_LENGTH].strip("-"))
        if len(parts) == MAX_DEPTH:
            break
    # Belt and braces against the column's own cap: four segments of
    # SEGMENT_MAX_LENGTH cannot reach it, but the two constants are free to move
    # independently and the column is not.
    return SEPARATOR.join(parts)[:TAG_MAX_LENGTH].rstrip(SEPARATOR)


def normalize_all(raw: list[str] | None, *, limit: int = 30) -> list[str]:
    """A whole tag list, canonicalised, deduped, and in the caller's order.

    Order is kept because a tag list is not a set to the person who typed it —
    the first tag is the one the platforms with a three-tag ceiling will get.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw or []:
        tag = normalize(raw_tag)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def segments(tag: str) -> list[str]:
    """The levels of *tag*: ``"a/b/c"`` → ``["a", "b", "c"]``."""
    return [part for part in tag.split(SEPARATOR) if part]


def leaf(tag: str) -> str:
    """The last level of *tag* — what a platform should be given.

    ``guides/deployment/docker`` is ``docker``. The parents are Herald's filing
    system and mean nothing on Dev.to, where the visible tag list is the reader's
    only navigation and ``guidesdeploymentdocker`` is not a tag anybody follows.
    """
    parts = segments(tag)
    return parts[-1] if parts else ""


def ancestors(tag: str) -> list[str]:
    """Every tag *tag* is filed under, nearest last, excluding itself.

    ``"a/b/c"`` → ``["a", "a/b"]``. An empty list for a top-level tag.
    """
    parts = segments(tag)
    return [SEPARATOR.join(parts[: i + 1]) for i in range(len(parts) - 1)]


def expand(tags: list[str]) -> list[str]:
    """*tags* plus every ancestor, sorted, deduped.

    What a filter runs against: asking for ``guides`` has to find the piece
    tagged ``guides/deployment``, and the piece's stored list does not contain
    the word ``guides``. Doing the expansion at read time rather than storing the
    ancestors means renaming a parent does not have to rewrite its children's
    rows — see :func:`rename_in`, which rewrites the prefix instead.
    """
    out: set[str] = set()
    for tag in tags:
        if not tag:
            continue
        out.add(tag)
        out.update(ancestors(tag))
    return sorted(out)


def is_within(tag: str, parent: str) -> bool:
    """Whether *tag* is *parent* or sits underneath it.

    A string prefix is not enough on its own: ``guides-advanced`` starts with
    ``guides`` and is a different tag. The separator has to be the next
    character.
    """
    return tag == parent or tag.startswith(parent + SEPARATOR)


def rename_in(tags: list[str], old: str, new: str) -> list[str]:
    """*tags* with *old* — and everything under it — re-parented onto *new*.

    ``rename_in(["guides/docker"], "guides", "howto")`` is ``["howto/docker"]``.
    The subtree moves with the node, which is the only reading of "rename a tag"
    that leaves the tree looking like the one the user was pointing at.

    Renaming onto a tag that already exists is a *merge*, and needs no special
    case: the result is deduped, so a piece that was tagged both ends up tagged
    once. That is also why this returns a list rather than mutating — the caller
    compares it against what was there to decide whether the row changed at all.

    A ``new`` that would push a tag past :data:`MAX_DEPTH` is re-normalised like
    any other, so the deepest levels are dropped rather than the write failing.
    A merge that collides after that truncation is still a merge.

    Each stored tag is canonicalised *before* the match, not after. The stored
    lists predate this module and hold whatever the model and the editor wrote —
    ``Guides/Docker`` is in the tree under ``guides/docker`` and has to be in the
    rename under it too. Matching the raw string instead would rename the tidy
    rows and leave the untidy ones behind, which is the half-done outcome a
    vocabulary tool cannot afford.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = normalize(raw)
        moved = new + tag[len(old) :] if is_within(tag, old) else tag
        canonical = normalize(moved)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


@dataclass
class TagNode:
    """One tag in the account's tree, with what hangs off it."""

    #: The full path — ``guides/deployment``, not ``deployment``.
    tag: str
    #: Pieces carrying this exact tag.
    direct: int = 0
    #: Pieces carrying this tag or anything under it. ``direct`` for a leaf.
    #: This is the number a facet list shows, because clicking a parent in a
    #: tree shows the subtree.
    total: int = 0
    children: list[TagNode] = field(default_factory=list)

    @property
    def depth(self) -> int:
        """How many levels this tag has. ``1`` for a top-level tag."""
        return len(segments(self.tag))

    def as_dict(self) -> dict:
        """The wire shape, children nested."""
        return {
            "tag": self.tag,
            "label": leaf(self.tag),
            "depth": self.depth,
            "direct": self.direct,
            "total": self.total,
            "children": [child.as_dict() for child in self.children],
        }


@dataclass
class TagTree:
    """Every tag this account uses, and whether the scan saw everything."""

    roots: list[TagNode] = field(default_factory=list)
    #: Distinct tags found, ancestors included.
    distinct: int = 0
    #: Pieces read. Never more than :data:`TREE_SCAN_LIMIT`.
    scanned: int = 0
    #: True when the account has more pieces than the scan was allowed to read,
    #: so the counts are a floor rather than a total. Reported rather than
    #: silently capped: a tag tree that quietly stops counting at five thousand
    #: is a tree somebody plans a rename against.
    truncated: bool = False

    def as_dict(self) -> dict:
        """The wire shape, roots nested."""
        return {
            "roots": [node.as_dict() for node in self.roots],
            "distinct": self.distinct,
            "scanned": self.scanned,
            "truncated": self.truncated,
        }


def build_tree(tag_lists: list[list[str]]) -> TagTree:
    """The tree for a set of already-normalised tag lists.

    Split from :func:`tree` so the shape can be tested without a database, and
    so the same assembly serves a caller that has the rows for another reason.

    A parent with no piece of its own still appears: an account whose only tag
    is ``guides/deployment`` has a ``guides`` node with ``direct=0`` and
    ``total=1``. Leaving it out would draw a forest of orphans and make the
    hierarchy invisible in exactly the account that has just started using one.
    """
    direct: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for tags in tag_lists:
        canonical = {t for t in (normalize(raw) for raw in tags) if t}
        direct.update(canonical)
        # ``expand`` per piece, not per tag: a piece tagged both ``guides`` and
        # ``guides/docker`` must count once against ``guides``, not twice.
        total.update(expand(sorted(canonical)))

    nodes = {tag: TagNode(tag=tag, direct=direct.get(tag, 0), total=count)
             for tag, count in total.items()}
    roots: list[TagNode] = []
    for tag in sorted(nodes):
        parents = ancestors(tag)
        parent = nodes.get(parents[-1]) if parents else None
        if parent is None:
            roots.append(nodes[tag])
        else:
            parent.children.append(nodes[tag])

    # Busiest first at every level, ties alphabetical — the order a facet list
    # wants, and stable, which matters because this is compared in tests.
    def order(node: TagNode) -> None:
        node.children.sort(key=lambda child: (-child.total, child.tag))
        for child in node.children:
            order(child)

    roots.sort(key=lambda node: (-node.total, node.tag))
    for root in roots:
        order(root)

    return TagTree(roots=roots, distinct=len(nodes))


def tree(
    db: Session,
    user_id: int,
    *,
    project_id: int | None = None,
    limit: int = TREE_SCAN_LIMIT,
) -> TagTree:
    """The account's tag tree, counted from its content.

    One column, no entities. ``Content`` carries the body, four JSON columns and
    an eagerly-loaded ``publications`` relationship, and this needs exactly one
    of those — so the query names ``Content.tags`` and nothing else. The
    difference is the whole page: an account with two thousand pieces would
    otherwise read two thousand article bodies to count some short strings.

    Ordered by id descending so that a truncated scan is a scan of the *recent*
    pieces, which is the half of a long history whose vocabulary is still in
    use.
    """
    scan = min(limit, TREE_SCAN_LIMIT)
    query = (
        select(Content.tags)
        .join(Project, Project.id == Content.project_id)
        .where(Project.user_id == user_id)
        .order_by(Content.id.desc())
        # One more than asked for, which is how ``truncated`` is answered
        # without a second COUNT over the same table.
        .limit(scan + 1)
    )
    if project_id is not None:
        query = query.where(Content.project_id == project_id)

    rows = list(db.scalars(query))
    truncated = len(rows) > scan
    rows = rows[:scan]

    built = build_tree([row or [] for row in rows])
    built.scanned = len(rows)
    built.truncated = truncated
    return built


@dataclass(frozen=True)
class Suggestion:
    """A proposed tag, and what it was proposed from."""

    tag: str
    #: 0-100. Comparable within one response and not across two — it is a
    #: ranking, not a probability, and presenting it as one would invite a
    #: threshold nobody calibrated.
    score: int
    #: One line, shown next to the tag. A suggestion a user cannot interrogate
    #: is one they are right to ignore — the same rule the cadence guide follows.
    reason: str

    def as_dict(self) -> dict:
        """The wire shape of one suggestion, reason included."""
        return {"tag": self.tag, "score": self.score, "reason": self.reason}


def _terms(content: Content) -> Counter[str]:
    """Significant words of a piece, counted.

    Title words count triple: a word in the title is what the piece is about by
    the author's own declaration, and a body long enough to say anything says
    most words at least once.

    Reuses :func:`app.services.dedup.tokens` for the tokenizing and the stopword
    list rather than growing a second one. That function exists to decide when
    two headlines are the same piece, which is the same judgement about which
    words carry subject matter — and a second stopword list is a second thing to
    keep in step.
    """
    counts: Counter[str] = Counter()
    body = seo.strip_markdown(content.body_markdown or "")
    for word in _WORD.findall(body.casefold()):
        if word not in dedup.STOPWORDS and not word.isdigit():
            counts[word] += 1
    for word in dedup.tokens(content.title or ""):
        if not word.isdigit():
            counts[word] += 3
    return counts


def suggest(
    db: Session,
    user_id: int,
    content: Content,
    *,
    limit: int = SUGGESTION_LIMIT,
) -> list[Suggestion]:
    """Tags worth adding to *content*, ranked, with the reason for each.

    Four sources, and the ordering between them is the design:

    1. **A tag this account already uses whose leaf appears in the piece.**
       Ranked first and scored highest, because agreeing with the existing
       vocabulary is the entire job — this is the source that produces
       ``guides/deployment`` instead of a fifth spelling of it. A hierarchical
       tag matched this way is proposed with its parents intact.
    2. **The project's tech stack**, where the piece mentions it. The stack is a
       list the user curated by hand, so a match is a strong signal and needs no
       frequency threshold.
    3. **The piece's own SEO keywords.** Already normalised, already chosen for
       this piece, and usually two or three of them.
    4. **Frequent words in the body**, at :data:`MIN_TERM_FREQUENCY` or more.
       The weakest source and the last resort, for a piece in a project with no
       history at all — which is the account that most needs a starting point
       and the one where every other source is empty.

    Tags the piece already carries are never proposed, at any depth: a piece
    tagged ``guides/deployment`` gets neither that nor ``guides``. Nor is a tag
    proposed twice from two sources — the first, strongest reason wins, which is
    why the sources are walked in this order.
    """
    already = set(expand(normalize_all(list(content.tags or []))))
    counts = _terms(content)
    proposals: dict[str, Suggestion] = {}

    def propose(raw: str, score: int, reason: str) -> None:
        tag = normalize(raw)
        if not tag or tag in already or tag in proposals:
            return
        proposals[tag] = Suggestion(tag=tag, score=min(score, 100), reason=reason)

    # (1) The account's own vocabulary. Read from the tree rather than a second
    # query so the scan bound above applies here too.
    known = _flatten(tree(db, user_id).roots)
    for node in known:
        label = leaf(node.tag)
        if counts.get(label):
            propose(
                node.tag,
                80 + min(node.total, 20),
                f"You use this tag on {node.total} other "
                f"{'piece' if node.total == 1 else 'pieces'}, and this one "
                f"mentions {label}.",
            )

    # (2) The stack the user curated. ``project`` is already loaded — every
    # caller reached this piece through an ownership check that walked it.
    for entry in content.project.tech_stack or []:
        term = normalize(entry)
        if term and counts.get(leaf(term)):
            propose(term, 70, f"{entry} is in this project's stack and the piece uses it.")

    # (3) The piece's own keywords.
    for keyword in seo.normalize_keywords(list(content.keywords or [])):
        propose(keyword, 55, "One of this piece's SEO keywords.")

    # (4) Whatever the piece keeps saying.
    for word, count in counts.most_common(limit * 3):
        if count >= MIN_TERM_FREQUENCY:
            propose(word, 30 + min(count, 20), f"Used {count} times in this piece.")

    ranked = sorted(proposals.values(), key=lambda s: (-s.score, s.tag))
    return ranked[:limit]


def _flatten(nodes: list[TagNode]) -> list[TagNode]:
    """Every node in a tree, parents before children."""
    out: list[TagNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.children))
    return out


__all__ = [
    "MAX_DEPTH",
    "MIN_TERM_FREQUENCY",
    "SEGMENT_MAX_LENGTH",
    "SEPARATOR",
    "SUGGESTION_LIMIT",
    "TREE_SCAN_LIMIT",
    "Suggestion",
    "TagNode",
    "TagTree",
    "ancestors",
    "build_tree",
    "expand",
    "is_within",
    "leaf",
    "normalize",
    "normalize_all",
    "rename_in",
    "segments",
    "suggest",
    "tree",
]
