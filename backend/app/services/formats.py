"""Output shapes: a piece is not always an article.

Every content type Herald had was 450 to 1200 words of Markdown prose with ``##``
headings. That is one shape, and the generator's prompt, its length budget and
its "is this usable?" checks were all written for it. Asking the same code path
for a Twitter thread produced a blog post with the headings removed.

So a :class:`ContentFormat` is the shape, derived from the type. Three of them:

``article``
    What Herald already did. Prose, subheadings, measured in words.
``thread``
    A sequence of posts, each standing on its own and each under a platform's
    character limit. Measured in posts, not words.
``changelog``
    Entries grouped under Added / Changed / Fixed. Not prose at all: a reader
    scans it for the line that affects them, and every sentence of framing
    around that line makes it slower to scan.

**The canonical storage is still Markdown**, one column, for all three. A thread
is its posts separated by blank lines; a changelog is ``##`` sections of bullet
lists. This is not a compromise — it is what lets the whole publish path,
the editor, the SEO audit and the diff view keep working unchanged, and it means
``TwitterAdapter.build_thread`` splits a native thread on exactly the boundaries
its author intended rather than guessing at paragraphs.

**Model output is repaired, not rejected.** A 340-character post in a thread is
a good thread with one long post in it. Splitting it at a sentence boundary
costs nothing and saves a generation; refusing the whole piece and falling back
to the static template costs the user everything they were about to publish.
"""
from __future__ import annotations

import re
from enum import Enum

from app.models.content import ContentType


class ContentFormat(str, Enum):
    """The shape of a piece, as opposed to its subject."""

    ARTICLE = "article"
    THREAD = "thread"
    CHANGELOG = "changelog"


#: Which type produces which shape. Everything that predates this module is an
#: article, which is why the mapping is a lookup with a default rather than a
#: field on the enum: adding a content type should not require choosing a shape.
FORMAT_FOR: dict[ContentType, ContentFormat] = {
    ContentType.SOCIAL_THREAD: ContentFormat.THREAD,
    ContentType.CHANGELOG: ContentFormat.CHANGELOG,
}


def format_of(content_type: ContentType | str) -> ContentFormat:
    """The shape a content type produces. Unknown types are articles."""
    try:
        key = ContentType(content_type)
    except ValueError:
        return ContentFormat.ARTICLE
    return FORMAT_FOR.get(key, ContentFormat.ARTICLE)


# --------------------------------------------------------------------------- #
# Threads                                                                      #
# --------------------------------------------------------------------------- #

#: The tightest limit among the platforms a thread goes to. Threads are written
#: once and posted everywhere, so the shortest limit is the only safe one to
#: write against — Mastodon's 500 would produce posts X silently refuses.
THREAD_POST_LIMIT = 280

#: Beyond this, a thread is an article somebody has pressed Enter in.
MAX_THREAD_POSTS = 25

#: A "thread" of one post is a post. Two is the minimum that earns the shape.
MIN_THREAD_POSTS = 2

#: Numbering the author or the model added: "1/", "3/7", "2.". Stripped on the
#: way in, because Herald's publish adapters number the thread themselves and
#: two numbering schemes on one post is worse than either.
_NUMBERING = re.compile(r"^\s*(?:\d+\s*/\s*\d*|\d+[.)])\s+")

#: Where a post may be cut. Sentence end first; a cut mid-sentence reads as a
#: truncation even when the next post continues it.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def parse_thread(body: str) -> list[str]:
    """The posts in a thread body, unnumbered and stripped.

    Blank lines separate posts. A post's own internal line breaks survive —
    a two-line post with a code snippet in it is a normal thing to write.
    """
    posts = []
    for block in (body or "").split("\n\n"):
        text = _NUMBERING.sub("", block.strip())
        # A markdown bullet or heading marker is scaffolding from a model that
        # reached for article shape; the words after it are the post.
        text = re.sub(r"^\s*(?:[-*+]\s+|#{1,6}\s+)", "", text)
        if text.strip():
            posts.append(text.strip())
    return posts


def split_post(text: str, limit: int = THREAD_POST_LIMIT) -> list[str]:
    """Break one over-long post into posts that fit, losing no words.

    Sentence boundaries first, then whitespace, then — for a single word longer
    than the limit, which is a URL or nothing — a hard cut, because the
    alternative is a post that cannot be published at all.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    current = ""
    for piece in _tokens(text, limit):
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        # `current` is necessarily non-empty here, and again at the tail below.
        # Both follow from _tokens' contract: every piece is non-empty and no
        # longer than the limit, and there is at least one for the non-empty
        # text this line is only reached with. An empty `current` would mean
        # candidate == piece, which would then have fit. Guarding anyway would
        # swallow a broken tokeniser instead of showing it — see
        # test_the_tokeniser_contract_split_post_relies_on.
        parts.append(current)
        current = piece
    parts.append(current)
    return parts


def _tokens(text: str, limit: int) -> list[str]:
    """Sentences where they fit, words where they don't, chunks where they must.

    Every token is non-empty and at most *limit* long; :func:`split_post`
    depends on both. ``filter`` drops empty sentences rather than an ``if``
    inside the loop because _SENTENCE_END cannot actually produce one — it
    matches ``\\s+`` after a stop, so it never fires at position 0 (nothing
    precedes it), never at the end (the caller strips), and never twice in a
    row (the run of whitespace is greedy). It stays as a filter so a change to
    that regex cannot start emitting blank posts.
    """
    tokens: list[str] = []
    sentences = (sentence.strip() for sentence in _SENTENCE_END.split(text))
    for sentence in filter(None, sentences):
        if len(sentence) <= limit:
            tokens.append(sentence)
            continue
        for word in sentence.split():
            if len(word) <= limit:
                tokens.append(word)
            else:
                tokens.extend(word[i : i + limit] for i in range(0, len(word), limit))
    return tokens


def normalize_thread(body: str, *, limit: int = THREAD_POST_LIMIT) -> str:
    """Canonical thread Markdown: unnumbered posts, one per block, all in limit.

    The repair pass described in the module docstring. Idempotent, so running it
    on an already-clean thread changes nothing.
    """
    posts: list[str] = []
    for post in parse_thread(body):
        posts.extend(split_post(post, limit))
    return render_thread(posts[:MAX_THREAD_POSTS])


def render_thread(posts: list[str]) -> str:
    """Posts back into the canonical Markdown body."""
    return "\n\n".join(post.strip() for post in posts if post.strip())


def thread_problems(body: str, *, limit: int = THREAD_POST_LIMIT) -> list[str]:
    """What is wrong with this thread, in the words the editor shows.

    Runs on stored content rather than model output — by the time a thread is
    stored the repair pass has already run, so anything here came from a human
    typing in the editor.
    """
    posts = parse_thread(body)
    problems = []
    if len(posts) < MIN_THREAD_POSTS:
        problems.append(
            f"A thread needs at least {MIN_THREAD_POSTS} posts — separate them "
            "with a blank line."
        )
    for index, post in enumerate(posts, start=1):
        if len(post) > limit:
            problems.append(
                f"Post {index} is {len(post)} characters. The limit is {limit}."
            )
    if len(posts) > MAX_THREAD_POSTS:
        problems.append(
            f"{len(posts)} posts is past the {MAX_THREAD_POSTS} Herald will "
            "publish. The tail will be dropped."
        )
    return problems


# --------------------------------------------------------------------------- #
# Changelogs                                                                   #
# --------------------------------------------------------------------------- #

#: The Keep a Changelog vocabulary, in the order it renders. A fixed, small set
#: is the point of the format: a reader who has seen one changelog can scan any
#: other one, and that only holds if "Added" is always called Added.
CHANGELOG_SECTIONS: tuple[str, ...] = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
)

#: Words a model reaches for that mean one of the six. Mapped rather than
#: allowed through, so "Bug Fixes" and "Fixed" do not become two sections that
#: sort apart and read as different things.
_SECTION_ALIASES: dict[str, str] = {
    "add": "Added", "adds": "Added", "added": "Added", "new": "Added",
    "new features": "Added", "features": "Added", "additions": "Added",
    "change": "Changed", "changes": "Changed", "changed": "Changed",
    "improvements": "Changed", "improved": "Changed", "updates": "Changed",
    "deprecate": "Deprecated", "deprecated": "Deprecated",
    "remove": "Removed", "removed": "Removed", "removals": "Removed",
    "fix": "Fixed", "fixes": "Fixed", "fixed": "Fixed",
    "bug fixes": "Fixed", "bugfixes": "Fixed", "bugs": "Fixed",
    "security": "Security", "security fixes": "Security",
}

#: How many entries one section keeps. A changelog listing sixty commits under
#: "Fixed" is a git log, and nobody reads a git log.
MAX_ENTRIES_PER_SECTION = 20

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


def canonical_section(name: str) -> str | None:
    """The Keep a Changelog section *name* means, or None if it means none."""
    cleaned = re.sub(r"[^a-z ]", "", (name or "").strip().lower()).strip()
    if not cleaned:
        return None
    if cleaned.title() in CHANGELOG_SECTIONS:
        return cleaned.title()
    return _SECTION_ALIASES.get(cleaned)


def parse_changelog(body: str) -> list[tuple[str, list[str]]]:
    """A changelog body as ``(section, entries)`` pairs, in document order.

    Entries outside any recognised section are collected under ``Changed`` —
    the honest default, and better than dropping a line the model wrote.
    """
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current = None

    def add(section: str, entry: str) -> None:
        if section not in sections:
            sections[section] = []
            order.append(section)
        if entry not in sections[section]:
            sections[section].append(entry)

    for line in (body or "").split("\n"):
        heading = _HEADING.match(line)
        if heading:
            current = canonical_section(heading.group(1))
            continue
        bullet = _BULLET.match(line)
        text = bullet.group(1).strip() if bullet else line.strip()
        if not text:
            continue
        # Prose between headings is a model explaining itself. In a changelog
        # that is noise, but a non-bulleted line under a real section is
        # usually just a bullet whose dash went missing.
        if not bullet and current is None:
            continue
        add(current or "Changed", text)

    return [(name, sections[name]) for name in order]


#: Conventional-commit types, and the section each belongs under. This is what
#: makes a changelog worth having when every provider is down: commit subjects
#: already carry their own classification, and reading it costs nothing.
_COMMIT_TYPES: dict[str, str] = {
    "feat": "Added",
    "feature": "Added",
    "add": "Added",
    "fix": "Fixed",
    "bugfix": "Fixed",
    "hotfix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "style": "Changed",
    "chore": "Changed",
    "build": "Changed",
    "ci": "Changed",
    "docs": "Changed",
    "test": "Changed",
    "revert": "Removed",
    "remove": "Removed",
    "deprecate": "Deprecated",
    "security": "Security",
}

#: ``feat(triggers)!: add RSS polling`` → type ``feat``, subject after the colon.
_CONVENTIONAL = re.compile(r"^\s*([a-zA-Z]+)\s*(?:\([^)]*\))?\s*!?\s*:\s*(.+)$")

#: Leading verbs, for the projects that do not write conventional commits.
_LEADING_VERBS: dict[str, str] = {
    "add": "Added", "added": "Added", "adds": "Added", "introduce": "Added",
    "create": "Added", "implement": "Added", "support": "Added",
    "fix": "Fixed", "fixed": "Fixed", "fixes": "Fixed", "correct": "Fixed",
    "resolve": "Fixed", "repair": "Fixed", "patch": "Fixed",
    "remove": "Removed", "removed": "Removed", "removes": "Removed",
    "drop": "Removed", "delete": "Removed", "revert": "Removed",
    "deprecate": "Deprecated", "deprecated": "Deprecated",
    "secure": "Security", "harden": "Security",
}


def classify_change(text: str) -> tuple[str, str]:
    """The section a change belongs under, and the change without its prefix.

    Conventional-commit type first, then the leading verb, then ``Changed`` —
    which is the honest answer for "something happened and it was not clear
    what kind of something".
    """
    subject = (text or "").strip().lstrip("-*+ ").strip()
    if not subject:
        return "Changed", ""

    match = _CONVENTIONAL.match(subject)
    if match:
        section = _COMMIT_TYPES.get(match.group(1).lower())
        if section:
            # The type prefix is scaffolding for other developers; a changelog
            # reader wants the subject.
            return section, match.group(2).strip()

    first = re.split(r"[\s:(]", subject, maxsplit=1)[0].lower()
    return _LEADING_VERBS.get(first, "Changed"), subject


def changelog_from_items(items: list[str] | tuple[str, ...]) -> str:
    """A changelog assembled from raw change lines, with no model involved.

    What the generator falls back to, and the reason the fallback for this one
    shape is worth publishing rather than merely worth rewriting.
    """
    grouped: dict[str, list[str]] = {}
    for item in items or ():
        section, subject = classify_change(item)
        if not subject:
            continue
        # Sentence case: commit subjects are lowercase by convention and a
        # changelog reads as a list of sentences.
        entry = subject[0].upper() + subject[1:]
        grouped.setdefault(section, [])
        if entry not in grouped[section]:
            grouped[section].append(entry)
    return render_changelog(list(grouped.items()))


def render_changelog(sections: list[tuple[str, list[str]]]) -> str:
    """Sections back into Markdown, in the canonical order."""
    ordered = sorted(
        sections,
        key=lambda pair: (
            CHANGELOG_SECTIONS.index(pair[0])
            if pair[0] in CHANGELOG_SECTIONS
            else len(CHANGELOG_SECTIONS)
        ),
    )
    blocks = []
    for name, entries in ordered:
        kept = [entry for entry in entries if entry.strip()][:MAX_ENTRIES_PER_SECTION]
        if not kept:
            continue
        lines = "\n".join(f"- {entry.strip()}" for entry in kept)
        blocks.append(f"## {name}\n\n{lines}")
    return "\n\n".join(blocks)


def normalize_changelog(body: str) -> str:
    """Canonical changelog Markdown: known sections, canonical order, no prose."""
    return render_changelog(parse_changelog(body))


def unrecognised_headings(body: str) -> list[str]:
    """Headings in *body* that name no changelog section, in document order.

    Read off the body rather than off :func:`parse_changelog`, which is where
    the information goes to die: that function resolves every heading through
    :func:`canonical_section` and files whatever it cannot place under
    ``Changed``, so by the time it returns, the name the author actually typed
    is gone. ``changelog_problems`` looked for unknown names in its output and
    therefore could never find one — the branch was unreachable, and an author
    who wrote ``## Enhancements`` got a clean panel while their entries were
    quietly being read as ``Changed``.
    """
    out: list[str] = []
    for line in (body or "").split("\n"):
        heading = _HEADING.match(line)
        if not heading:
            continue
        name = heading.group(1).strip()
        if name and canonical_section(name) is None and name not in out:
            out.append(name)
    return out


def changelog_problems(body: str) -> list[str]:
    """What is wrong with this changelog, in the words the editor shows."""
    sections = parse_changelog(body)
    if not sections:
        return ["A changelog needs at least one entry under a heading like ## Fixed."]

    problems = []
    unknown = unrecognised_headings(body)
    if unknown:
        problems.append(
            f"{', '.join(unknown)} is not a changelog section, so anything under "
            f"it is being read as Changed. Use one of: "
            f"{', '.join(CHANGELOG_SECTIONS)}."
        )
    for name, entries in sections:
        if len(entries) > MAX_ENTRIES_PER_SECTION:
            problems.append(
                f"{name} has {len(entries)} entries — only the first "
                f"{MAX_ENTRIES_PER_SECTION} will be kept."
            )
    return problems


# --------------------------------------------------------------------------- #
# What the rest of the engine needs to know                                    #
# --------------------------------------------------------------------------- #

#: Below this, an *article* generation failed regardless of what the provider
#: said. Only articles have a word floor: see :func:`too_thin_to_store`.
MIN_ARTICLE_WORDS = 60

#: Shape-specific rules for the prompt, replacing the article rules that used to
#: be unconditional. The JSON envelope is unchanged for all three: one
#: ``body_markdown`` string, because a second envelope shape is a second parser
#: and a second set of ways for a free model to get it subtly wrong.
PROMPT_RULES: dict[ContentFormat, str] = {
    ContentFormat.ARTICLE: (
        '- Markdown body. Start at "## " for section headings — the title is '
        "separate, so the body must not repeat it as an H1."
    ),
    ContentFormat.THREAD: (
        "- The body is a thread: one post per paragraph, separated by a blank "
        f"line, {MIN_THREAD_POSTS} to 8 posts.\n"
        f"- Every post must be under {THREAD_POST_LIMIT} characters on its own.\n"
        "- Do not number the posts — Herald numbers them when it publishes.\n"
        "- No Markdown headings, and no bullet lists. The first post has to earn "
        "the second one; the last says what to do next."
    ),
    ContentFormat.CHANGELOG: (
        "- The body is a changelog. Markdown '## ' headings chosen only from: "
        f"{', '.join(CHANGELOG_SECTIONS)}. Omit any section with nothing in it.\n"
        "- Under each heading, '- ' bullets. One change per bullet, present "
        "tense, starting with a verb: '- Add RSS triggers', not '- We have "
        "added RSS trigger support in this release'.\n"
        "- No introduction, no conclusion, no prose between the headings. A "
        "changelog is scanned, not read."
    ),
}


def normalize(body: str, content_type: ContentType | str) -> str:
    """Put *body* into the canonical form for its shape.

    **For model output only.** Normalizing is lossy by design — a changelog's
    prose between headings is dropped, because that is exactly the padding the
    format exists to prevent — and doing that to something a person typed would
    delete their work while they were looking at it. Hand edits get
    :func:`problems` instead, which says what is wrong and changes nothing.
    """
    shape = format_of(content_type)
    if shape == ContentFormat.THREAD:
        return normalize_thread(body)
    if shape == ContentFormat.CHANGELOG:
        return normalize_changelog(body)
    return body


def too_thin_to_store(body: str, content_type: ContentType | str) -> str | None:
    """Why *body* is too thin to be a piece of this shape, or None if it isn't.

    **Run on normalized model output**, and the reason the article word floor is
    not simply applied to all three shapes: a word count measures the wrong
    thing for two of them. ``## Fixed / - The parser crash`` is a complete and
    correct changelog at six words, and a three-post thread is a thread at nine.
    Counting words there rejects good work for being the shape it was asked to
    be.

    So each shape is judged in its own unit — words for an article, posts for a
    thread, entries for a changelog — and what survives is a structural check:
    did normalizing leave anything of this shape behind? A model that answered a
    changelog request with "Thanks for reading!" leaves nothing, and that is a
    failed generation no matter how many words it used.

    The string is a log line, not a user-facing message; the editor gets
    :func:`problems`.
    """
    shape = format_of(content_type)

    if shape == ContentFormat.THREAD:
        posts = parse_thread(body)
        if len(posts) < MIN_THREAD_POSTS:
            return f"{len(posts)} posts, minimum {MIN_THREAD_POSTS}"
        return None

    if shape == ContentFormat.CHANGELOG:
        entries = sum(len(items) for _, items in parse_changelog(body))
        if not entries:
            return "no changelog entries"
        return None

    words = len((body or "").split())
    if words < MIN_ARTICLE_WORDS:
        return f"{words} words, minimum {MIN_ARTICLE_WORDS}"
    return None


def problems(body: str, content_type: ContentType | str) -> list[str]:
    """Everything wrong with *body* for its shape. Empty for an article.

    The non-destructive half of the pair. What the editor shows for a piece a
    person has been typing in, where :func:`normalize` would be vandalism.
    """
    shape = format_of(content_type)
    if shape == ContentFormat.THREAD:
        return thread_problems(body)
    if shape == ContentFormat.CHANGELOG:
        return changelog_problems(body)
    return []


__all__ = [
    "CHANGELOG_SECTIONS",
    "FORMAT_FOR",
    "MAX_ENTRIES_PER_SECTION",
    "MAX_THREAD_POSTS",
    "MIN_ARTICLE_WORDS",
    "MIN_THREAD_POSTS",
    "PROMPT_RULES",
    "THREAD_POST_LIMIT",
    "ContentFormat",
    "canonical_section",
    "changelog_from_items",
    "changelog_problems",
    "classify_change",
    "format_of",
    "normalize",
    "normalize_changelog",
    "normalize_thread",
    "parse_changelog",
    "parse_thread",
    "problems",
    "render_changelog",
    "render_thread",
    "split_post",
    "thread_problems",
    "too_thin_to_store",
    "unrecognised_headings",
]
