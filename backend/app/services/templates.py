"""Filling a template in.

The substitution itself is three lines of :mod:`re`. Everything else here is the
decisions around it, which is where a template system is actually won or lost:

**One pass, never two.** A value containing ``{{something}}`` is text, not a
placeholder. Re-scanning the output would let a feed entry's body expand into
whatever the author happened to name a variable — a template injection with a
stranger's RSS feed on the other end of it.

**Unknown placeholders are refused on write, not at render.** ``{{versoin}}`` is
a typo, and the moment to say so is while the author is looking at the editor
that produced it. Deferring it means a Friday-morning schedule quietly renders
the literal text ``{{versoin}}`` into a published post. Validation therefore
lives in :func:`unknown_placeholders`, which the schema layer calls.

**Built-ins are always available.** A template is worth most when a trigger is
filling it in, and that only works if the event's own facts have names. So
``project.*``, ``date.*`` and ``signal.*`` resolve without being declared —
which is also why declared variables may not shadow them.

**An empty optional value takes its line with it.** The alternative is a
published post containing "Read more: " with nothing after it. A line whose only
non-whitespace content came from placeholders that all resolved empty is
dropped; a line with any literal text of its own is kept, because that text was
something the author chose to write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.models.content import BODY_MARKDOWN_MAX_LENGTH, TITLE_MAX_LENGTH

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.template import ContentTemplate
    from app.services.signals import TriggerSignal

#: ``{{ name }}``, ``{{name}}``, ``{{signal.headline}}``. Dots are namespaces
#: for the built-ins; a declared variable is a bare identifier.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")

#: Namespaces Pulse fills in. A declared variable may not use these names.
BUILTIN_NAMESPACES = ("project", "date", "signal")

#: Every built-in, with the one-line description the UI lists them by. Kept
#: here rather than in the router so the list and the resolver cannot drift.
BUILTINS: dict[str, str] = {
    "project.name": "The project's name.",
    "project.slug": "The project's URL slug.",
    "project.description": "The project's one-paragraph description.",
    "project.url": "The project's live URL, or its repo if it has no site.",
    "project.repo": "The watched repository, as owner/name.",
    "project.tech_stack": "The project's tech stack, comma separated.",
    "date.today": "Today's date, as 2026-08-01.",
    "date.long": "Today's date, as 1 August 2026.",
    "date.year": "The current year.",
    "date.month": "The current month's name.",
    "signal.headline": "What the trigger saw happen. Empty when there is no trigger.",
    "signal.summary": "The trigger's prose — release notes, a feed entry's body.",
    "signal.url": "Where a reader can see the thing itself.",
    "signal.source": "Where the trigger got it: 'RSS Changelog', 'GitHub r2st/Herald'.",
    "signal.items": "The trigger's bullet points, one per line.",
}

#: How much of one substituted value is kept. A 40 KB feed body pasted into a
#: 200-word template is not the piece the author designed.
VALUE_LIMIT = 5000

#: How many bullet lines `signal.items` expands to.
ITEMS_LIMIT = 40

#: Ceilings on the *output*, which is a different question from the ceilings on
#: the inputs — and the one that was missing.
#:
#: Bounding each value at ``VALUE_LIMIT`` bounds nothing about the result,
#: because a placeholder may repeat. A 50,000-character ``body_template`` holds
#: about ten thousand copies of ``{{v}}``, so one 48 KB request rendered a 40 MB
#: body: past the 1 MB body-size middleware, into a ``Text`` column with no width
#: to stop it, and from there into every adapter request, every SEO audit and
#: every load of the editor for that piece. The title had the narrower version of
#: the same problem and a worse landing: ``Content.title`` is ``String(300)`` and
#: ``slug`` is ``String(320)``, so a rendered title over either is a PostgreSQL
#: ``DataError`` — and the slug is a unique-index key, where an over-long value
#: exceeds the btree entry limit as well.
#:
#: These are the same numbers the hand-written path has always enforced through
#: ``ContentCreate``; a template is not a way around them.
TITLE_LIMIT = TITLE_MAX_LENGTH
BODY_LIMIT = BODY_MARKDOWN_MAX_LENGTH


@dataclass
class Rendered:
    """A filled-in template, and what was missing while filling it."""

    title: str
    body: str
    #: Declared, required, and had no value from any source. The render still
    #: happened — a preview of a half-filled template is more useful than an
    #: error — but a caller writing a real piece should refuse on this.
    missing: list[str] = field(default_factory=list)
    #: Which placeholders actually resolved to something non-empty. Lets the UI
    #: show "3 of 5 filled" without re-parsing.
    filled: list[str] = field(default_factory=list)
    #: Which of ``title``/``body`` came out over its limit and was clipped.
    #:
    #: Same split as ``missing``, and for the same reason: a preview renders
    #: anyway, because seeing the first 200 KB of what a template produces is
    #: how an author finds out it produces too much. A caller writing a real
    #: piece refuses — silently storing a truncated body would make the row
    #: disagree with the template that is supposed to explain it.
    over_limit: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Every placeholder resolved. Says nothing about :attr:`over_limit`."""
        return not self.missing


def placeholders(*texts: str) -> list[str]:
    """Every distinct placeholder across *texts*, in first-seen order."""
    seen: dict[str, None] = {}
    for text in texts:
        for match in PLACEHOLDER.finditer(text or ""):
            seen.setdefault(match.group(1), None)
    return list(seen)


def unknown_placeholders(declared: list[str], *texts: str) -> list[str]:
    """Placeholders that are neither declared nor built in.

    The check the schema layer runs on write. Sorted, because it becomes an
    error message and a stable one is easier to read than a positional one.
    """
    known = set(declared) | set(BUILTINS)
    return sorted({name for name in placeholders(*texts) if name not in known})


def reserved_names(declared: list[str]) -> list[str]:
    """Declared names that collide with a built-in namespace.

    A variable called ``project`` would make ``{{project.name}}`` ambiguous, and
    one called ``date.today`` would silently never be used.
    """
    bad = []
    for name in declared:
        head = name.split(".", 1)[0]
        if head in BUILTIN_NAMESPACES:
            bad.append(name)
    return sorted(set(bad))


def builtin_context(
    project: Project | None = None,
    signal: TriggerSignal | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """The always-available values, as flat dotted keys.

    Every key in :data:`BUILTINS` is present, empty when there is nothing to put
    in it. A template that references ``{{signal.url}}`` and is run by hand
    should render a blank there and lose the line, not the literal placeholder.
    """
    moment = now or datetime.now(UTC)

    context = {key: "" for key in BUILTINS}
    context.update(
        {
            "date.today": moment.strftime("%Y-%m-%d"),
            # No leading zero on the day: "1 August 2026", not "01 August".
            "date.long": f"{moment.day} {moment.strftime('%B %Y')}",
            "date.year": str(moment.year),
            "date.month": moment.strftime("%B"),
        }
    )

    if project is not None:
        context.update(
            {
                "project.name": project.name or "",
                "project.slug": project.slug or "",
                "project.description": project.description or "",
                "project.url": project.live_url or project.repo_url or "",
                "project.repo": project.repo_full_name or "",
                "project.tech_stack": ", ".join(project.tech_stack or []),
            }
        )

    if signal is not None:
        items = list(signal.items or ())[:ITEMS_LIMIT]
        context.update(
            {
                "signal.headline": signal.headline or "",
                "signal.summary": signal.summary or "",
                "signal.url": signal.url or "",
                "signal.source": signal.source or "",
                "signal.items": "\n".join(f"- {item}" for item in items),
            }
        )

    return context


def resolve(
    template: ContentTemplate,
    values: dict[str, Any] | None = None,
    *,
    project: Project | None = None,
    signal: TriggerSignal | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The full substitution context, and the required variables still empty.

    Precedence runs from most specific to least: a supplied value beats the
    variable's declared default, which beats empty. Built-ins are separate and
    cannot be overridden — a caller passing ``{"project.name": "..."}`` is
    working around the template rather than filling it in.
    """
    supplied = values or {}
    context = builtin_context(project, signal, now=now)
    missing: list[str] = []

    for entry in template.variables or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue

        raw = supplied.get(name)
        text = "" if raw is None else str(raw).strip()
        if not text:
            text = str(entry.get("default") or "")
        if not text and entry.get("required"):
            missing.append(name)

        context[name] = text[:VALUE_LIMIT]

    return context, missing


def substitute(text: str, context: dict[str, str]) -> tuple[str, list[str]]:
    """Fill *text* in from *context*, and say which placeholders had content.

    One pass: a replacement containing ``{{...}}`` is left alone. A placeholder
    with no entry in the context renders empty rather than raising — write-time
    validation is what stops that from happening, and a render that explodes
    mid-publish is worse than one that leaves a gap.
    """
    filled: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = str(context.get(name, ""))[:VALUE_LIMIT]
        if value:
            filled.append(name)
        return value

    return PLACEHOLDER.sub(replace, text or ""), filled


#: What makes a line a *label* for its placeholder rather than a sentence
#: containing one. ``Read more: {{url}}`` and ``Version — {{v}}`` are captions
#: for a value; without the value they are litter. ``Shipped {{version}}`` is a
#: sentence, and the author's words stay whether or not it fills in.
LABEL_SUFFIX = re.compile(r"[:\-–—=>]\s*$")


def drop_empty_lines(original: str, rendered: str) -> str:
    """Remove lines whose only content was placeholders that came out empty.

    Compares line by line against the *original* so the decision is about what
    the author wrote, not about what happened to render. A line is disposable
    when, with its placeholders removed, what is left is either

    * nothing of the author's own — ``- {{item}}``, ``({{date.long}})`` — or
    * a label for the missing value: literal text ending in ``:``, ``-``, ``—``
      or ``=``, as in ``Read more: {{signal.url}}``.

    Anything else is kept, because the words in it were a choice. ``Changelog
    {{version}}`` renders as ``Changelog`` rather than disappearing.
    """
    source_lines = (original or "").split("\n")
    rendered_lines = rendered.split("\n")
    if len(source_lines) != len(rendered_lines):
        # A substituted value contained a newline, so the lines no longer
        # correspond. Multi-line values are legitimate (`{{signal.items}}`), and
        # guessing at the alignment would delete real content.
        return rendered

    kept = []
    for source, line in zip(source_lines, rendered_lines, strict=True):
        if not PLACEHOLDER.search(source):
            kept.append(line)
            continue

        literal = PLACEHOLDER.sub("", source).strip()
        # Every placeholder on this line came out empty exactly when what is
        # left of the rendered line is the literal text that surrounded them.
        # Compared on collapsed whitespace, since removing a value leaves the
        # spaces that were on either side of it.
        if " ".join(line.split()) != " ".join(literal.split()):
            kept.append(line)
            continue

        if not re.search(r"[A-Za-z0-9]", literal) or LABEL_SUFFIX.search(literal):
            continue

        # Kept, but tidied: the gap the empty value left behind is not
        # something the author typed. Leading indentation is, so it stays.
        indent = line[: len(line) - len(line.lstrip())]
        kept.append(indent + " ".join(line.split()))
    return "\n".join(kept)


def collapse_blank_runs(text: str) -> str:
    """At most one blank line in a row, and none at the ends.

    Dropping lines leaves holes, and three blank lines in Markdown is a visible
    gap in the published post.
    """
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render(
    template: ContentTemplate,
    values: dict[str, Any] | None = None,
    *,
    project: Project | None = None,
    signal: TriggerSignal | None = None,
    now: datetime | None = None,
) -> Rendered:
    """Fill *template* in. Never raises."""
    context, missing = resolve(
        template, values, project=project, signal=signal, now=now
    )

    title, title_filled = substitute(template.title_template or "", context)
    body, body_filled = substitute(template.body_template or "", context)

    body = collapse_blank_runs(drop_empty_lines(template.body_template or "", body))
    title = " ".join(title.split())

    # Clipped after the whitespace and empty-line passes, not before: those
    # passes shrink the text, so measuring first would refuse renders that
    # actually fit.
    over_limit = []
    if len(title) > TITLE_LIMIT:
        over_limit.append("title")
        title = title[:TITLE_LIMIT]
    if len(body) > BODY_LIMIT:
        over_limit.append("body")
        body = body[:BODY_LIMIT]

    return Rendered(
        title=title,
        body=body,
        missing=missing,
        filled=sorted(set(title_filled) | set(body_filled)),
        over_limit=over_limit,
    )


def excerpt_from(body: str, limit: int = 200) -> str:
    """A one-line summary of a rendered body, for the content row's excerpt.

    A literal template produces no model output and therefore no excerpt, and
    an empty one costs a piece its preview text on every platform that shows
    one. The first real paragraph is the honest answer.
    """
    for block in (body or "").split("\n\n"):
        # Skip headings, list scaffolding and images looking for actual prose.
        cleaned = " ".join(
            line.strip()
            for line in block.split("\n")
            if line.strip() and not line.strip().startswith(("#", "!", ">", "|"))
        ).strip()
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        if not cleaned:
            continue
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"
    return ""


__all__ = [
    "BUILTINS",
    "BUILTIN_NAMESPACES",
    "PLACEHOLDER",
    "Rendered",
    "builtin_context",
    "excerpt_from",
    "placeholders",
    "render",
    "reserved_names",
    "resolve",
    "substitute",
    "unknown_placeholders",
]
