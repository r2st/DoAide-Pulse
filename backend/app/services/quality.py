"""How readable a piece is, how much of it is code, and one number for both.

:mod:`app.services.seo` measures a piece as a *search result*: does it have a
meta description, does the focus keyword reach the title, does the heading order
make sense. Every one of those checks is about the envelope. None of them reads
the prose, which is why a generated post can score 100 there and still be one of
the two things an LLM writes when the brief is thin:

* **Unreadable.** Four hundred words of subordinate clauses at a reading ease a
  standards document would be embarrassed by. Flesch-Kincaid is the oldest and
  most boring way to measure that, which is exactly why it is the right one — it
  is arithmetic over syllables and sentence lengths, it needs no model, and two
  runs on the same body give the same answer, so it can sit in a gate.
* **All code and no glue.** Six fenced blocks with a sentence of transition
  between them. The word count looks fine (``strip_markdown`` drops the fences,
  so it never counted them), the SEO audit is content, and the post reads like a
  paste. The code-to-text ratio is the only thing here that looks at what a
  reader actually gets.

:func:`report` folds those two together with the SEO score into a single 0–100
:class:`QualityReport`, which is what the DRAFT→REVIEW gate reads — see
:data:`app.config.Settings.content_quality_min_score`. One number rather than
three thresholds, because the failure this guards against is a piece that is
*mediocre in every direction at once*, and three separate gates each set low
enough not to be a nuisance let exactly that through.

**Deterministic, and not a judgement about writing.** Flesch-Kincaid does not
know what good prose is; it knows sentence length and syllable count. A short,
dense, excellent paragraph scores badly and a fluent piece of nonsense scores
well. That is a limit of the measure, not a bug in it, and it is why the
threshold ships low: this catches the piece nobody should have to read, not the
piece a stylist would improve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services import seo

#: Words a body needs before its reading ease means anything.
#:
#: The formula divides by the sentence count and by the word count, so a
#: two-sentence social post produces a number with the precision of a
#: measurement and the stability of a coin toss — re-word one clause of a
#: tweet and the "reading ease" moves thirty points. Pulse writes tweets and
#: LinkedIn posts from the same pipeline as articles, so this is the common
#: case rather than an edge one, and the honest answer for those is that the
#: piece has no reading ease. See :class:`Readability`, whose fields are
#: ``None`` below this, and :func:`report`, which drops the component from the
#: combined score rather than scoring it zero.
MIN_WORDS_FOR_READABILITY = 30

#: The reading-ease band a technical post should land in. Below 40 the prose is
#: dense enough that a reader skims to the code; above 80 it has been written
#: for a much broader audience than a changelog has.
#:
#: Asymmetric on purpose — see :func:`readability_points`. Too dense is the
#: failure that actually reaches the review queue; too simple is a style
#: quibble, and charging both at the same rate would put a plain, clear post in
#: front of a human for being plain and clear.
READING_EASE_TARGET_MIN = 40.0
READING_EASE_TARGET_MAX = 80.0

#: How much of a post may be code before it stops being a post. Two fifths is
#: generous — a tutorial genuinely is mostly listings — and the deduction above
#: it is gradual rather than a cliff, so a code-heavy walkthrough loses a few
#: points and a paste with a paragraph on top loses most of them.
CODE_RATIO_IDEAL_MAX = 0.40

#: What the combined score is made of. SEO carries half because it is the
#: broadest of the three (a dozen checks against these two), and the readability
#: share is the larger of the remaining two because a body nobody can read is
#: worse than a body that is mostly listings.
SEO_WEIGHT = 0.5
READABILITY_WEIGHT = 0.3
CODE_WEIGHT = 0.2

_WORD = re.compile(r"[A-Za-z0-9']+")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")
_SENTENCE_SPLIT = re.compile(r"[.!?]+")

#: A fenced block, *including* one left unterminated. ``.*?```` alone matches
#: nothing when the model forgets the closing fence, and the body that follows
#: a stray fence is exactly the body most likely to be all code — so the one
#: case the ratio most wants to catch was the one it silently scored zero.
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
#: Inline code. Single line by design: a backtick pair spanning a paragraph
#: break is an unclosed span, not a code span, and treating it as one would
#: charge the rest of the post as code.
_INLINE_CODE = re.compile(r"`[^`\n]+`")

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HEADING_MARK = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
_ORDERED = re.compile(r"^\s{0,3}\d+[.)]\s+", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", re.M)
_RESIDUAL_MARKUP = re.compile(r"[*_~`\[\]()#|]")
_WHITESPACE = re.compile(r"\s+")


def prose_lines(body_markdown: str) -> list[str]:
    """The body as plain lines, code and markup removed, line breaks kept.

    :func:`app.services.seo.strip_markdown` does nearly this and collapses the
    result to a single line, which is right for a word count and wrong here.
    Sentence length is half of both Flesch formulas, and a document whose
    headings and bullets have been run together into one string has no sentence
    boundaries where a reader sees the most obvious ones — a six-bullet list
    under an H2 reads as one 90-word sentence, and the piece is reported as
    unreadable because of its formatting.

    Fences are dropped rather than stripped to their contents: code is not
    prose, and a body's readability is a fact about the words around the code.
    That is also what makes the two measures here independent — the ratio is
    the one that notices there is a lot of code, and it would be double-counted
    if the code also dragged the reading ease down.
    """
    text = _FENCE.sub("\n", body_markdown or "")
    text = _INLINE_CODE.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _RULE.sub("", text)
    text = _HEADING_MARK.sub("", text)
    text = _BULLET.sub("", text)
    text = _ORDERED.sub("", text)
    text = _QUOTE.sub("", text)
    text = _RESIDUAL_MARKUP.sub("", text)
    return [line.strip() for line in text.splitlines() if line.strip()]


def syllables(word: str) -> int:
    """A syllable count for one word, by the usual vowel-group heuristic.

    Never zero for a word with letters in it, because both formulas divide by
    the word count and multiply by this — a token contributing nothing would be
    a free word, and a body of numbers would read as infinitely simple.
    """
    cleaned = word.lower().strip("'")
    if not cleaned:
        return 0
    count = len(_VOWEL_GROUP.findall(cleaned))
    # A trailing "e" is usually silent ("code", "phrase") but is not in "-le"
    # ("table") and is not the whole of a doubled vowel ("see"), and dropping it
    # from a one-group word would leave the word with none.
    if count > 1 and cleaned.endswith("e") and not cleaned.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


@dataclass(frozen=True)
class Readability:
    """Flesch-Kincaid over a body's prose, plus what it was computed from.

    ``reading_ease`` and ``grade_level`` are ``None`` together, for a body under
    :data:`MIN_WORDS_FOR_READABILITY` words — see that constant. The counts are
    reported either way, because "12 words" is the explanation for the nulls.
    """

    words: int
    sentences: int
    syllables: int
    #: Flesch Reading Ease, clamped to 0–100. The raw formula is unbounded in
    #: both directions — a body of one 60-word sentence of polysyllables goes
    #: negative — and a scale that runs off both ends of itself cannot be shown
    #: on a dial or mixed into a score.
    reading_ease: float | None
    #: Flesch-Kincaid Grade Level, floored at 0 for the same reason. Not
    #: capped: "grade 22" is a meaningful thing to say about a sentence.
    grade_level: float | None


def readability(body_markdown: str) -> Readability:
    """Measure *body_markdown*'s prose. Never raises, never divides by zero."""
    lines = prose_lines(body_markdown)
    words = _WORD.findall(" ".join(lines))
    word_count = len(words)

    sentence_count = 0
    for line in lines:
        parts = [part for part in _SENTENCE_SPLIT.split(line) if part.strip()]
        # A heading or a bullet with no full stop is still one unit a reader
        # finishes, so it counts as a sentence rather than as none.
        sentence_count += max(1, len(parts))

    syllable_count = sum(syllables(word) for word in words)

    if word_count < MIN_WORDS_FOR_READABILITY or sentence_count == 0:
        return Readability(
            words=word_count,
            sentences=sentence_count,
            syllables=syllable_count,
            reading_ease=None,
            grade_level=None,
        )

    words_per_sentence = word_count / sentence_count
    syllables_per_word = syllable_count / word_count
    ease = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
    grade = 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59
    return Readability(
        words=word_count,
        sentences=sentence_count,
        syllables=syllable_count,
        reading_ease=round(max(0.0, min(100.0, ease)), 1),
        grade_level=round(max(0.0, grade), 1),
    )


def _dense(text: str) -> int:
    """Characters that are not whitespace. The unit both halves of the ratio use.

    Counting raw length instead would make the ratio a measurement of
    indentation: a code block is far more whitespace per character than a
    paragraph is, so a listing indented four spaces would read as more code
    than the same listing flush left.
    """
    return len(_WHITESPACE.sub("", text))


def code_ratio(body_markdown: str) -> float:
    """Share of the body, by dense characters, that is code. 0.0–1.0.

    Fenced blocks and inline spans, counted once each: the fences are removed
    before inline spans are looked for, so a backtick pair inside a listing is
    not charged twice. Indented code blocks are not counted — four leading
    spaces is also how a nested list continues, and treating those as code
    would report a bulleted post as a paste.
    """
    body = body_markdown or ""
    total = _dense(body)
    if not total:
        return 0.0
    fenced = sum(_dense(block) for block in _FENCE.findall(body))
    inline = sum(_dense(span) for span in _INLINE_CODE.findall(_FENCE.sub(" ", body)))
    return round(min(1.0, (fenced + inline) / total), 4)


def readability_points(reading_ease: float | None) -> int | None:
    """The reading ease as a 0–100 component of the combined score.

    Full marks inside :data:`READING_EASE_TARGET_MIN` –
    :data:`READING_EASE_TARGET_MAX`, and outside it a linear fall — two points
    per point of ease below the band, one point per point above. The asymmetry
    is the constants' docstring: dense is the failure, simple is a preference.

    ``None`` in and ``None`` out, so a piece too short to measure is not
    silently scored zero for it.
    """
    if reading_ease is None:
        return None
    if reading_ease < READING_EASE_TARGET_MIN:
        return max(0, round(100 - 2 * (READING_EASE_TARGET_MIN - reading_ease)))
    if reading_ease > READING_EASE_TARGET_MAX:
        return max(0, round(100 - (reading_ease - READING_EASE_TARGET_MAX)))
    return 100


def code_points(ratio: float) -> int:
    """The code ratio as a 0–100 component. Full marks up to the ideal max.

    No floor on prose: a post with no code in it is not worse for having none,
    which is why this is one-sided where :func:`readability_points` is not.
    """
    if ratio <= CODE_RATIO_IDEAL_MAX:
        return 100
    over = (ratio - CODE_RATIO_IDEAL_MAX) / (1.0 - CODE_RATIO_IDEAL_MAX)
    return max(0, round(100 * (1.0 - over)))


@dataclass(frozen=True)
class QualityReport:
    """One piece measured every way Pulse measures a piece deterministically."""

    score: int
    seo_score: int
    readability: Readability
    code_ratio: float
    #: The two derived components, so a caller showing the score can also show
    #: which part of it is low. ``readability_points`` is ``None`` exactly when
    #: ``readability.reading_ease`` is.
    readability_points: int | None
    code_points: int

    def as_dict(self) -> dict[str, Any]:
        """The response shape. Flat, because it is rendered as a panel."""
        return {
            "score": self.score,
            "seo_score": self.seo_score,
            "reading_ease": self.readability.reading_ease,
            "grade_level": self.readability.grade_level,
            "readability_points": self.readability_points,
            "code_ratio": self.code_ratio,
            "code_points": self.code_points,
            "words": self.readability.words,
            "sentences": self.readability.sentences,
        }


def report(
    *,
    title: str,
    body_markdown: str,
    meta_description: str,
    keywords: list[str],
    cover_image_url: str | None = None,
    focus_keyword: str = "",
    slug: str = "",
) -> QualityReport:
    """Score a piece 0–100 on structure, readability and code density.

    The weights are :data:`SEO_WEIGHT`, :data:`READABILITY_WEIGHT` and
    :data:`CODE_WEIGHT`, and they always sum to one. When a piece is too short
    to have a reading ease the readability weight moves to the code component
    rather than the component being scored zero — a two-line social post is not
    a badly written one, and scoring the absence would put every tweet Pulse
    writes below any threshold worth setting. Where it moves *to* is the part
    that took a bug to get right; the comment on that branch has it.

    Takes the same keyword arguments as :func:`app.services.seo.seo_score` and
    passes them straight through, so there is one spelling of "what a piece is"
    across both modules and no chance of the gate scoring a different envelope
    from the one the editor's panel audits.
    """
    seo_points = seo.seo_score(
        title=title,
        body_markdown=body_markdown,
        meta_description=meta_description,
        keywords=keywords,
        cover_image_url=cover_image_url,
        focus_keyword=focus_keyword,
        slug=slug,
    )
    measured = readability(body_markdown)
    ratio = code_ratio(body_markdown)
    read_points = readability_points(measured.reading_ease)
    code = code_points(ratio)

    if read_points is None:
        # The readability weight goes to the *code* component, not back into
        # the pool. Spreading it proportionally is the obvious thing and it is
        # wrong, because it hands most of it to the SEO score — and the SEO
        # score is exactly what a thin piece is best at. A body of thirty
        # fenced lines and the words "Run it." has a good title, a good
        # description and its focus keyword everywhere it belongs; it scored 70
        # there, took 71% of the total on that alone, and landed on 50 against a
        # floor of 50.
        #
        # There is one question left that still has an answer for such a body,
        # and it is the one that matters: how much of this is code. A genuine
        # social post takes full marks for it and is unaffected — a tweet is not
        # made worse by having no code in it — so the redistribution only bites
        # where the missing prose and the code are the same fact.
        parts: list[tuple[float, int]] = [
            (SEO_WEIGHT, seo_points),
            (CODE_WEIGHT + READABILITY_WEIGHT, code),
        ]
    else:
        parts = [
            (SEO_WEIGHT, seo_points),
            (READABILITY_WEIGHT, read_points),
            (CODE_WEIGHT, code),
        ]
    combined = sum(w * p for w, p in parts)

    # The one case the weighted mean gets plainly wrong. A body with nothing in
    # it loses five points of SEO score for being under 300 words, has no
    # reading ease to fail, and takes full marks for code density because there
    # is no code — which came to 61/100 for the empty string. Both of the
    # generous components are answering "is there too much of the wrong thing",
    # and neither can see that there is nothing at all. An empty body is not a
    # piece that scored well on the parts that were checked; it is not a piece.
    if not _dense(body_markdown or ""):
        combined = 0.0

    return QualityReport(
        score=max(0, min(100, round(combined))),
        seo_score=seo_points,
        readability=measured,
        code_ratio=ratio,
        readability_points=read_points,
        code_points=code,
    )


def report_for(content: object) -> QualityReport:
    """:func:`report` for a :class:`~app.models.content.Content` row.

    The field list is the same one :func:`app.routers.content._to_detail` hands
    the SEO audit. It is here rather than inlined at the two call sites because
    those two are the editor's panel and the review gate, and a piece must not
    be able to score one thing on screen and another on the way through.
    """
    return report(
        title=getattr(content, "title", "") or "",
        body_markdown=getattr(content, "body_markdown", "") or "",
        meta_description=getattr(content, "meta_description", "") or "",
        keywords=list(getattr(content, "keywords", None) or []),
        cover_image_url=getattr(content, "cover_image_url", None),
        focus_keyword=getattr(content, "focus_keyword", "") or "",
        slug=getattr(content, "slug", "") or "",
    )


__all__ = [
    "CODE_RATIO_IDEAL_MAX",
    "CODE_WEIGHT",
    "MIN_WORDS_FOR_READABILITY",
    "READABILITY_WEIGHT",
    "READING_EASE_TARGET_MAX",
    "READING_EASE_TARGET_MIN",
    "SEO_WEIGHT",
    "QualityReport",
    "Readability",
    "code_points",
    "code_ratio",
    "prose_lines",
    "readability",
    "readability_points",
    "report",
    "report_for",
    "syllables",
]
