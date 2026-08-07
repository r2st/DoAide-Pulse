"""The content engine: turn a project (plus what just shipped) into a post.

Every generation asks the model for a **JSON envelope** rather than raw prose.
Two reasons, both learned from the free tier:

* the reasoning models return chain-of-thought in ``content`` often enough that
  raw prose is a coin flip, and a JSON object survives being wrapped in one;
* a post needs a title, body, excerpt, meta description, tags *and* a
  self-assessed confidence, and asking for them separately is six round-trips
  through a rate-limited free endpoint instead of one.

When the whole provider chain is down, :func:`generate` still returns a piece —
a deterministic template built from the project record and the repo activity.
It is not a good post, and it is marked ``confidence=0.0`` so the autopilot will
never publish it unreviewed, but it is a draft a human can open and fix. The
alternative, a 500 in a background task, loses the trigger entirely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models.content import TARGET_WORDS, ContentType
from app.models.project import Project, Tone
from app.services import ai, formats, seo
from app.services.github_client import RepoActivity
from app.services.signals import TriggerSignal, from_repo_activity

logger = logging.getLogger(__name__)

#: How each tone should read, in the words the prompt uses. Kept here rather
#: than in the enum so the copy can be tuned without a migration.
_TONE_GUIDANCE: dict[Tone, str] = {
    Tone.TECHNICAL: (
        "Write for engineers. Be concrete: name the libraries, show short code "
        "snippets where they earn their space, and prefer a specific number to "
        "an adjective. No hype, no exclamation marks, no 'game-changing'."
    ),
    Tone.CASUAL: (
        "Write like a developer explaining a side project to a friend who also "
        "codes. Contractions, first person, short paragraphs. Still accurate — "
        "casual is a register, not permission to be vague."
    ),
    Tone.MARKETING: (
        "Lead with the reader's problem and what changes for them. Keep it "
        "credible: claims must trace back to a real feature. No superlatives "
        "you cannot support, and never invent a customer, a metric, or a quote."
    ),
}

#: What each content type is *for*. The single biggest lever on output quality —
#: without it every type comes back as the same mid-length blog post.
_TYPE_GUIDANCE: dict[ContentType, str] = {
    ContentType.TUTORIAL: (
        "A step-by-step tutorial that takes the reader from nothing to a working "
        "result. Number the steps. Every code block must be runnable as written. "
        "End with what to try next."
    ),
    ContentType.ANNOUNCEMENT: (
        "A release announcement. Lead with what is new and who should care, then "
        "the details, then how to get it. Short. Do not pad it to blog length."
    ),
    ContentType.FEATURE_SPOTLIGHT: (
        "A deep look at one feature: the problem it solves, how it works, and "
        "what it looks like to use. One feature — resist listing the others."
    ),
    ContentType.COMPARISON: (
        "An honest comparison against the alternatives a reader is actually "
        "weighing. Name real trade-offs, including where the alternative wins. "
        "A comparison that finds no downsides reads as an advert and converts "
        "like one."
    ),
    ContentType.HOW_TO: (
        "A focused how-to answering one specific question. Get to the answer in "
        "the first two paragraphs, then explain it."
    ),
    ContentType.SOCIAL_THREAD: (
        "A thread. The first post is the whole point in one sentence and must "
        "work alone, because most readers see nothing else. Each post after it "
        "carries exactly one idea. No thread-bait ('a 🧵 on...'), no numbering, "
        "and nothing that only makes sense if you read the previous post."
    ),
    ContentType.CHANGELOG: (
        "A changelog for this release. Only what actually changed, one change "
        "per line, written from the reader's side: what they can now do, not "
        "what was refactored to allow it. If the brief does not say a thing "
        "changed, it does not go in."
    ),
}

_SYSTEM_PROMPT = (
    "You are a senior developer-marketing writer. You write accurate, specific "
    "technical content about software products. You never invent features, "
    "benchmarks, customers or quotes — if a fact is not in the brief, you leave "
    "it out. You always reply with a single JSON object and nothing else."
)


@dataclass
class GeneratedContent:
    """What the engine produces, before it becomes a ``Content`` row."""

    title: str
    body_markdown: str
    excerpt: str
    meta_description: str
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: The primary SEO keyword — the first project keyword, used to drive the
    #: SEO audit score (density, first-paragraph, subheading checks).
    focus_keyword: str = ""
    #: The model's own read on whether this is publishable unreviewed. 0.0 for
    #: the template fallback, so it can never clear the auto-publish gate.
    confidence: float = 0.0
    provider: str | None = None
    model: str | None = None
    #: True when the provider chain failed and this is the static template.
    is_fallback: bool = False


def content_from_generated(
    db: Any,
    *,
    project_id: int,
    content_type: ContentType,
    generated: GeneratedContent,
    status: Any,
    source: dict,
    **extra: Any,
) -> Any:
    """Build a ``Content`` row from a ``GeneratedContent``.

    Centralises the field mapping that was previously duplicated in the content
    router (generate, write_from_idea) and in autopilot_tasks.
    """
    from app.models.content import Content, unique_content_slug  # avoid circular

    return Content(
        project_id=project_id,
        content_type=content_type,
        status=status,
        title=generated.title,
        slug=unique_content_slug(db, project_id, generated.title),
        body_markdown=generated.body_markdown,
        excerpt=generated.excerpt,
        meta_description=generated.meta_description,
        keywords=generated.keywords,
        tags=generated.tags,
        focus_keyword=generated.focus_keyword,
        confidence=generated.confidence,
        generated_by_provider=generated.provider,
        generated_by_model=generated.model,
        source=source,
        **extra,
    )


def _as_signal(
    activity: RepoActivity | None, signal: TriggerSignal | None
) -> TriggerSignal | None:
    """Whichever of the two the caller supplied, as a signal.

    ``activity=`` predates the trigger system and stays supported: the GitHub
    path is one trigger kind out of four now, not a special case, and the
    conversion is cheap enough to do on every call rather than asking three
    call sites to do it themselves.
    """
    if signal is not None:
        return signal
    if activity is None or not activity.has_news:
        return None
    return from_repo_activity(activity)


def _activity_digest(
    activity: RepoActivity | None,
    *,
    max_commits: int = 25,
    signal: TriggerSignal | None = None,
) -> str:
    """What happened, as prompt-ready text, or "" when nothing did."""
    resolved = _as_signal(activity, signal)
    return resolved.digest(max_items=max_commits) if resolved else ""


def _build_prompt(
    project: Project,
    content_type: ContentType,
    *,
    activity: RepoActivity | None,
    instructions: str,
    signal: TriggerSignal | None = None,
) -> list[dict[str, str]]:
    brief = project.brief()
    tone = project.tone if isinstance(project.tone, Tone) else Tone(project.tone)
    target = TARGET_WORDS.get(content_type, 800)

    facts = [
        f"Project: {brief['name']}",
        f"What it is: {brief['description'] or '(no description on file)'}",
    ]
    if brief["tech_stack"]:
        facts.append(f"Built with: {', '.join(brief['tech_stack'])}")
    if brief["target_audience"]:
        facts.append(f"Who it is for: {brief['target_audience']}")
    if brief["live_url"]:
        facts.append(f"Live at: {brief['live_url']}")
    if brief["repo_url"]:
        facts.append(f"Source: {brief['repo_url']}")
    if brief["keywords"]:
        facts.append(f"Target keywords: {', '.join(brief['keywords'])}")

    resolved = _as_signal(activity, signal)
    digest = _activity_digest(activity, signal=signal)
    if digest:
        # Naming the source in the prompt matters: a model told "recent
        # development activity" invents engineering detail when what it was
        # actually handed is a status-page entry.
        label = (
            f"What just happened ({resolved.source})"
            if resolved and resolved.source
            else "What just happened"
        )
        facts.append(f"{label}:\n{digest}")
    if instructions.strip():
        facts.append("Extra direction from the author: " + instructions.strip())

    shape = formats.format_of(content_type)
    # A thread is measured in posts and a changelog in entries; only an article
    # has a word count worth asking for, and giving one to the other two is how
    # you get a blog post with the headings taken out.
    length = (
        f"Length: about {target} words.\n"
        if shape == formats.ContentFormat.ARTICLE
        else ""
    )

    user_prompt = f"""Write a {content_type.label.lower()} about this project.

{chr(10).join(facts)}

Format: {_TYPE_GUIDANCE[content_type]}
Voice: {_TONE_GUIDANCE[tone]}
{length}
Rules:
{formats.PROMPT_RULES[shape]}
- Only claim what the brief supports. No invented metrics, users or quotes.
- Weave the target keywords in naturally; do not stuff them.

Reply with exactly this JSON object and nothing else:
{{
  "title": "under 60 characters, contains the primary keyword",
  "body_markdown": "the full post",
  "excerpt": "one or two sentences, under 220 characters",
  "meta_description": "70-155 characters",
  "keywords": ["3-8 search keywords"],
  "tags": ["3-5 short platform tags, lowercase, no spaces"],
  "confidence": 0.0-1.0
}}

"confidence" is your own honest assessment of whether this is accurate and
polished enough to publish with no human edit. Be strict: below 0.8 it goes to
a review queue, which is the correct outcome whenever the brief was thin."""

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _fallback(
    project: Project,
    content_type: ContentType,
    activity: RepoActivity | None,
    signal: TriggerSignal | None = None,
) -> GeneratedContent:
    """A usable draft assembled from the record when every provider is down.

    Deliberately plain and obviously a stub: the failure mode to avoid is
    something that reads finished enough to publish by accident.
    """
    brief = project.brief()
    digest = _activity_digest(activity, max_commits=10, signal=signal)
    shape = formats.format_of(content_type)

    if shape != formats.ContentFormat.ARTICLE:
        return _fallback_shaped(
            shape, brief, content_type, _as_signal(activity, signal), digest
        )

    sections = [
        f"## What {brief['name']} is",
        brief["description"] or f"{brief['name']} is a work in progress.",
    ]
    if brief["tech_stack"]:
        sections += ["## How it is built", "Built with " + ", ".join(brief["tech_stack"]) + "."]
    if digest:
        sections += ["## What changed", digest]
    if brief["live_url"]:
        sections += ["## Try it", f"See it at <{brief['live_url']}>."]
    sections += [
        "---",
        "_Drafted by Herald from the project record: no AI provider was "
        "reachable at generation time. Rewrite before publishing._",
    ]

    body = "\n\n".join(sections)
    title = f"{brief['name']}: {content_type.label}"
    keywords = seo.normalize_keywords(brief["keywords"])
    return GeneratedContent(
        title=title,
        body_markdown=body,
        excerpt=seo.build_excerpt(body),
        meta_description=seo.build_meta_description("", fallback_body=body),
        keywords=keywords,
        tags=seo.normalize_keywords(brief["tech_stack"], extra=["devtools"])[:4],
        focus_keyword=keywords[0] if keywords else "",
        confidence=0.0,
        is_fallback=True,
    )


def _fallback_shaped(
    shape: formats.ContentFormat,
    brief: dict[str, Any],
    content_type: ContentType,
    signal: TriggerSignal | None,
    digest: str,
) -> GeneratedContent:
    """The provider-is-down draft for a thread or a changelog.

    The changelog case is the one worth reading. Commit subjects already carry
    their own classification — ``feat:``, ``fix:``, a leading verb — so a
    changelog can be assembled from the signal alone with no model involved at
    all, and the result is a real changelog rather than an obvious stub. It is
    the one fallback in Herald that is worth publishing rather than merely
    worth rewriting, which is exactly the point of having a shape that is a
    list of facts rather than a piece of prose.

    A thread is not so lucky: a hook is a writing problem, and there is no
    honest way to assemble one from a project record. So that fallback stays a
    stub and says so, in the post where it cannot be missed.
    """
    items = list(signal.items) if signal else []

    if shape == formats.ContentFormat.CHANGELOG:
        body = formats.changelog_from_items(items)
        if not body and digest:
            # No itemised changes, but something happened. One entry beats an
            # empty changelog, and the prose is the signal's own.
            body = formats.render_changelog(
                [("Changed", [line.strip() for line in digest.split("\n") if line.strip()][:10])]
            )
        headline = signal.headline if signal else ""
        title = f"{brief['name']} — {headline}" if headline else f"{brief['name']}: changelog"
    else:
        posts = [
            (signal.headline if signal and signal.headline else f"{brief['name']}: an update."),
            brief["description"] or f"{brief['name']} is a work in progress.",
        ]
        posts += [item for item in items[:5] if item.strip()]
        if brief["live_url"]:
            posts.append(f"See it at {brief['live_url']}")
        posts.append(
            "Drafted by Herald from the project record — no AI provider was "
            "reachable. Rewrite before posting."
        )
        body = formats.normalize_thread(formats.render_thread(posts))
        title = f"{brief['name']}: {content_type.label}"

    keywords = seo.normalize_keywords(brief["keywords"])
    return GeneratedContent(
        title=seo.truncate_at_sentence(title, 300),
        body_markdown=body,
        excerpt=seo.build_excerpt(body),
        meta_description=seo.build_meta_description("", fallback_body=body),
        keywords=keywords,
        tags=seo.normalize_keywords(brief["tech_stack"], extra=["devtools"])[:4],
        focus_keyword=keywords[0] if keywords else "",
        confidence=0.0,
        is_fallback=True,
    )


#: Tokens the free reasoning models burn on their scratchpad before emitting a
#: single character of answer. Measured, not guessed: `gpt-oss-20b:free` spent
#: 1487 of a 1490-token budget on `reasoning_tokens` and returned empty content,
#: and still wanted ~3400 when given 4000. The allowance is a flat addition
#: rather than a multiplier because the scratchpad's size tracks the difficulty
#: of the *instructions*, not the length of the requested output — a 450-word
#: announcement reasons about as hard as a 1200-word tutorial.
#:
#: Getting this wrong is expensive and silent: the response comes back
#: `finish_reason: length` with the JSON envelope cut off mid-string, which
#: looks exactly like a model that cannot follow instructions.
_REASONING_ALLOWANCE_TOKENS = 5000


def generate(
    project: Project,
    content_type: ContentType,
    *,
    activity: RepoActivity | None = None,
    signal: TriggerSignal | None = None,
    instructions: str = "",
) -> GeneratedContent:
    """Draft one piece of content. Never raises — falls back to a template.

    *activity* and *signal* are two spellings of "here is what happened":
    the first is a GitHub scan, the second is any trigger at all (see
    :mod:`app.services.signals`). Pass one; *signal* wins if both arrive.

    The long-form model is used for the types whose target length actually needs
    it; a 450-word announcement through the 120b model is slower for no gain.
    """
    target_words = TARGET_WORDS.get(content_type, 800)
    long_form = target_words >= 900
    model = (
        settings.openrouter_long_form_model if long_form else settings.openrouter_model
    )
    # The other configured OpenRouter model is the cheapest fallback there is:
    # free-tier quotas are metered per model, so a key that has spent its budget
    # on one of these still has one for the other. Wrong-sized rather than
    # absent is a trade worth making — the alternative when both are exhausted
    # is a template with confidence 0.0, which can never auto-publish.
    sibling = (
        settings.openrouter_model if long_form else settings.openrouter_long_form_model
    )
    # ~2.2 tokens per word covers the prose plus its JSON escaping; the flat
    # allowance covers the reasoning scratchpad. See the constant above.
    max_tokens = int(target_words * 2.2) + _REASONING_ALLOWANCE_TOKENS

    messages = _build_prompt(
        project,
        content_type,
        activity=activity,
        instructions=instructions,
        signal=signal,
    )

    try:
        payload, completion = ai.json_completion(
            messages,
            model=model,
            fallback_models=(sibling,),
            temperature=0.7,
            max_tokens=max_tokens,
        )
    except ai.AIError as exc:
        logger.warning(
            "content generation for project %s fell back to template: %s",
            project.id,
            exc,
        )
        return _fallback(project, content_type, activity, signal)

    return _assemble(payload, completion, project, content_type, activity, signal)


def _assemble(
    payload: dict[str, Any],
    completion: Any,
    project: Project,
    content_type: ContentType,
    activity: RepoActivity | None,
    signal: TriggerSignal | None = None,
) -> GeneratedContent:
    """Coerce and clean the model's JSON into a :class:`GeneratedContent`."""
    body = ai.as_str(payload.get("body_markdown"))
    title = ai.as_str(payload.get("title"))

    # Repair before judging. A thread with one 340-character post in it is a
    # good thread that needs splitting, not a failed generation — see the
    # module docstring in app.services.formats.
    body = formats.normalize(body, content_type)

    shape = formats.format_of(content_type)

    # A body that is chain-of-thought, or too thin to be a piece of this shape,
    # is a failed generation even though the request succeeded. "Too thin" is
    # measured in the shape's own unit — see formats.too_thin_to_store.
    reason = "empty" if not body else formats.too_thin_to_store(body, content_type)
    if reason or ai.looks_like_reasoning(body):
        logger.warning(
            "content generation for project %s returned unusable %s (%s) "
            "— using template",
            project.id,
            shape.value,
            reason or "reasoning transcript",
        )
        return _fallback(project, content_type, activity, signal)

    if not title or ai.looks_like_reasoning(title):
        title = f"{project.name}: {content_type.label}"

    keywords = seo.normalize_keywords(
        ai.as_str_list(payload.get("keywords")), extra=list(project.keywords or [])
    )
    excerpt = ai.as_str(payload.get("excerpt")) or seo.build_excerpt(body)
    meta = seo.build_meta_description(
        ai.as_str(payload.get("meta_description")), fallback_body=body
    )

    return GeneratedContent(
        title=seo.truncate_at_sentence(title, 300),
        body_markdown=body,
        excerpt=seo.truncate_at_sentence(seo.strip_markdown(excerpt), 220),
        meta_description=meta,
        keywords=keywords,
        # Platform tags are their own vocabulary: lowercase, no spaces, and
        # capped at 4 because Dev.to rejects a fifth.
        tags=[t.replace(" ", "") for t in ai.as_str_list(payload.get("tags"), limit=4)],
        # The first keyword is the focus — the one the audit scores against.
        focus_keyword=keywords[0] if keywords else "",
        confidence=ai.as_float(payload.get("confidence"), default=0.5),
        provider=completion.provider,
        model=completion.model,
    )


# --------------------------------------------------------------------------- #
# Ideas                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Idea:
    """A suggested subject, before anyone has written it."""

    content_type: ContentType
    headline: str
    rationale: str


def _fallback_ideas(
    project: Project,
    activity: RepoActivity | None,
    signal: TriggerSignal | None = None,
) -> list[Idea]:
    """Ideas derivable without a model — the obvious ones, which are often right."""
    ideas: list[Idea] = []
    if activity and activity.new_release:
        tag = activity.new_release.tag
        ideas.append(
            Idea(
                ContentType.ANNOUNCEMENT,
                f"{project.name} {tag} is out",
                f"Release {tag} was published and has not been written about.",
            )
        )
    if activity and activity.new_commits:
        ideas.append(
            Idea(
                ContentType.FEATURE_SPOTLIGHT,
                f"What's new in {project.name}",
                f"{len(activity.new_commits)} commits since the last piece.",
            )
        )
    if not ideas and signal is not None and signal.has_news:
        # A non-GitHub trigger has no commits to count, but its headline is
        # already a one-line statement of what happened — which is what an idea
        # is. Reusing it beats inventing a generic placeholder.
        ideas.append(
            Idea(
                signal.suggested_type,
                signal.headline[:300] or f"What's new in {project.name}",
                f"{signal.source} reported this and it has not been written about.",
            )
        )
    if not ideas:
        ideas.append(
            Idea(
                ContentType.HOW_TO,
                f"Getting started with {project.name}",
                "Every project needs a getting-started piece.",
            )
        )
    return ideas


def suggest_ideas(
    project: Project,
    *,
    activity: RepoActivity | None = None,
    signal: TriggerSignal | None = None,
    limit: int = 4,
) -> list[Idea]:
    """Subjects worth writing about for this project. Never raises."""
    brief = project.brief()
    digest = _activity_digest(activity, max_commits=15, signal=signal)

    prompt = f"""Suggest {limit} content ideas for this project.

Project: {brief['name']}
What it is: {brief['description'] or '(no description on file)'}
Built with: {', '.join(brief['tech_stack']) or 'unspecified'}
Audience: {brief['target_audience'] or 'developers'}
{('Recent activity:' + chr(10) + digest) if digest else ''}

Each idea must be something a reader would search for, not a topic only the
author cares about. Vary the type.

Reply with exactly this JSON object:
{{"ideas": [{{"content_type": "tutorial|announcement|feature_spotlight|comparison|how_to",
             "headline": "the working title",
             "rationale": "one sentence on why this is worth writing now"}}]}}"""

    try:
        payload, _ = ai.json_completion(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            # Small output, same large reasoning overhead — see
            # _REASONING_ALLOWANCE_TOKENS.
            max_tokens=_REASONING_ALLOWANCE_TOKENS,
        )
    except ai.AIError as exc:
        logger.info("idea generation for project %s fell back: %s", project.id, exc)
        return _fallback_ideas(project, activity, signal)[:limit]

    ideas: list[Idea] = []
    for raw in payload.get("ideas") or []:
        if not isinstance(raw, dict):
            continue
        headline = ai.as_str(raw.get("headline"))
        if not headline:
            continue
        try:
            content_type = ContentType(ai.as_str(raw.get("content_type")).lower())
        except ValueError:
            content_type = ContentType.FEATURE_SPOTLIGHT
        ideas.append(
            Idea(content_type, headline, ai.as_str(raw.get("rationale")))
        )

    return ideas[:limit] or _fallback_ideas(project, activity, signal)[:limit]


__all__ = ["GeneratedContent", "Idea", "generate", "suggest_ideas"]
