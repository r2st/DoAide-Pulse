"""Has Herald already written this?

Two different questions, asked at two different moments, and the difference
between them is the whole design.

**"Are these the same commits?"** is exact and cheap, and it can be asked
*before* the model is called. The autopilot records the shas it wrote about in
``Content.source``, so a scan that arrives holding a set of commits already
covered by a stored piece can stop there and spend nothing. This is the check
that saves a generation.

**"Does this say the same thing?"** is a judgement, and for the autopilot it can
only be asked *after* the model is called, because the thing being judged is the
title and the title is what the generation produces. So it cannot save the
generation — what it saves is the *publish*. A near-duplicate that is merely
parked in review costs a quota and wastes nobody's attention; the same piece
auto-published to Dev.to and Bluesky beside its twin is the failure that is
visible to readers and cannot be taken back.

Both are needed. The commit check alone misses the case the autopilot actually
produces — the watermark held for an outage or a threshold, the commits arrive
again enlarged, and the second generation writes the first one's article with a
different title. The similarity check alone would pay for that article every
time.

**Why the same similarity function as the idea backlog.** The comparison here is
:func:`is_restatement`, which came from :mod:`app.services.content_generator`,
where it deduplicates banked ideas. It is deliberately shared rather than
reimplemented: an idea and the piece written from it are the same headline at
two moments in its life, and two thresholds tuned separately would eventually
disagree about one string — a headline banked as new, then written, then judged
a duplicate of the piece the *previous* idea produced. One function, one
threshold, one answer.

**What is deliberately not deduplicated.** Nothing here compares bodies. Two
pieces with different titles and near-identical bodies are a real failure and a
much harder question — a body comparison would have to be robust to a
rewritten intro and a reordered section list, which is a similarity problem this
module's four lines of Jaccard are not the answer to. Titles are where the
duplication Herald actually produces shows up, because the title is generated
from the same brief that produced the last one.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Headline similarity                                                          #
# --------------------------------------------------------------------------- #

#: Words carrying no subject, dropped before two headlines are compared. Short
#: and deliberately so: this is not a stemmer, it is the handful of words that
#: differ between two phrasings of the same idea ("How to deploy Herald *to*
#: production" against the same headline with *into*) and never distinguish two
#: real ones.
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in",
    "into", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to",
    "what", "when", "where", "which", "why", "with", "you", "your",
})

#: Possessives and the ``'s`` contraction, dropped before tokenizing so
#: "Herald's caching layer" and "the caching layer in Herald" reduce to the same
#: words. Curly apostrophes included: the model emits them and a user typing a
#: headline on a Mac gets them by autocorrect.
_POSSESSIVE = re.compile(r"['’]s\b|['’]")

#: Kept together across a dot or hyphen so "2.0" and "double-post" stay one word
#: — a version number split into "2" and "0" would make every release headline
#: look like every other one.
_TOKEN = re.compile(r"[0-9a-z]+(?:[.\-][0-9a-z]+)*")

#: How much of two headlines' subject matter must coincide before the second is
#: taken as a restatement of the first. Tuned against the pair this has to keep
#: *apart*: "Getting started with Herald" and "Getting started with Herald Pro"
#: share three of four significant words — 0.75 — and are two different pieces.
SIMILARITY_THRESHOLD = 0.8


def tokens(headline: str) -> frozenset[str]:
    """The significant words of a headline, for comparing it against another."""
    plain = _POSSESSIVE.sub("", (headline or "").casefold())
    return frozenset(w for w in _TOKEN.findall(plain) if w not in STOPWORDS)


def is_restatement(candidate: str, existing: frozenset[str]) -> bool:
    """Whether *candidate* says what a headline with *existing* tokens already said."""
    candidate_tokens = tokens(candidate)
    if not candidate_tokens or not existing:
        return False
    # Under two significant words there is not enough of a subject to judge
    # overlap on: "Caching" would swallow "Caching" and nothing else usefully,
    # while any single shared word would score 1.0 against another one-word
    # headline. Exact match only, down there.
    if len(candidate_tokens) < 2 or len(existing) < 2:
        return candidate_tokens == existing
    overlap = len(candidate_tokens & existing) / len(candidate_tokens | existing)
    return overlap >= SIMILARITY_THRESHOLD


# --------------------------------------------------------------------------- #
# Provenance                                                                   #
# --------------------------------------------------------------------------- #

#: Where the commit shas a piece was written from are kept, inside
#: ``Content.source``. A key in the existing JSON column rather than a new one:
#: ``source`` is already the provenance record, it is already written by every
#: path that creates content, and a column would need a migration to store a
#: fact that is only ever read back through this module.
SOURCE_COMMITS_KEY = "commit_shas"

#: The most shas stored on one piece. A scan can arrive holding a full page of
#: a hundred, and the list is written into a JSON column that every listing
#: query and every editor render loads — see
#: :data:`app.models.content.BODY_MARKDOWN_MAX_LENGTH` for the same reasoning
#: applied to the body. Twenty is comfortably past
#: ``autopilot_commit_threshold`` and is enough to recognise a repeat: two scans
#: of an overlapping window share their *newest* commits, which are the ones
#: kept, because the list arrives newest-first.
MAX_STORED_COMMITS = 20

#: How much of a sha is stored. Seven is what a human reads and what git itself
#: abbreviates to; the full forty would quadruple the column for no more
#: distinguishing power at Herald's scale. Compared against other stored shas
#: only, never against GitHub, so the abbreviation is consistent on both sides.
_SHA_PREFIX = 7


def commit_shas(activity: Any) -> list[str]:
    """The abbreviated shas of the commits in a :class:`RepoActivity`.

    Duck-typed on ``new_commits`` rather than importing ``github_client``: the
    trigger path passes a signal and the autopilot passes an activity, and this
    module has no reason to know the difference between them beyond "does it
    carry commits".
    """
    commits = getattr(activity, "new_commits", None) or []
    out: list[str] = []
    for commit in commits:
        sha = str(getattr(commit, "sha", "") or "").strip().lower()
        if sha:
            out.append(sha[:_SHA_PREFIX])
        if len(out) >= MAX_STORED_COMMITS:
            break
    return out


def _stored_shas(source: Any) -> set[str]:
    """The shas recorded on one stored piece's ``source``, defensively.

    ``source`` is a JSON column written by several versions of this code and by
    the seeder, so everything in it is read as "might be anything": a piece
    written before this key existed has no key, and one written by a future
    version might have something other than a list under it.
    """
    if not isinstance(source, dict):
        return set()
    raw = source.get(SOURCE_COMMITS_KEY)
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower()[:_SHA_PREFIX] for item in raw if str(item).strip()}


# --------------------------------------------------------------------------- #
# The two questions                                                            #
# --------------------------------------------------------------------------- #


def _recent(db: Session, project_id: int) -> list[tuple[int, str, Any]]:
    """``(id, title, source)`` for this project's recent pieces, newest first.

    Three columns, not rows. ``Content`` carries the body, and a project's last
    two hundred bodies is tens of megabytes to answer a question about titles —
    the payload-width failure that
    ``tests/test_unused_publication_loads.py`` exists to catch elsewhere.

    Archived pieces are included on purpose. Somebody archiving a post has said
    they do not want it, which is not the same as saying they want it written
    again — and writing the twin of something just withdrawn is the most
    annoying version of this bug.
    """
    since = utcnow() - timedelta(days=settings.dedup_window_days)
    return [
        (row.id, row.title, row.source)
        for row in db.execute(
            select(Content.id, Content.title, Content.source)
            .where(Content.project_id == project_id, Content.created_at >= since)
            .order_by(Content.created_at.desc(), Content.id.desc())
            .limit(settings.dedup_compare_limit)
        ).all()
    ]


def duplicate_of(
    db: Session,
    project_id: int,
    *,
    title: str = "",
    shas: Iterable[str] = (),
) -> int | None:
    """Either question, in one call, over one query.

    The two checks share ``_recent``, and a caller that has both a title and a
    set of shas should not pay for the window twice. Commits first: it is the
    exact answer, and a piece that matches on shas matches whatever its title
    says.
    """
    recent = _recent(db, project_id)
    wanted = {s for s in (str(x).strip().lower()[:_SHA_PREFIX] for x in shas) if s}

    if wanted:
        for content_id, _title, source in recent:
            if wanted <= _stored_shas(source):
                return content_id

    if title.strip():
        for content_id, existing_title, _source in recent:
            if is_restatement(title, tokens(existing_title)):
                return content_id
    return None


__all__ = [
    "MAX_STORED_COMMITS",
    "SIMILARITY_THRESHOLD",
    "SOURCE_COMMITS_KEY",
    "STOPWORDS",
    "commit_shas",
    "duplicate_of",
    "is_restatement",
    "tokens",
]
