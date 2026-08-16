"""Generate a piece from a signal, then decide where it goes.

This is the half of the autopilot that has nothing to do with GitHub: write the
draft, run the quality gates, and either park it for review or queue it for
publication. It was inlined in ``autopilot_tasks`` when a repo push was the only
thing that could start a piece. Triggers made that a problem — an RSS entry and
a Friday morning deserve exactly the same gates, and a second copy of them would
drift within a release.

The gates only apply to an *unreviewed* publish, which is deliberate:

* **Dead links.** An auto-publish is the one place a fabricated URL reaches an
  audience unchallenged. A dead link is not a failed generation — the piece is
  fine and one link is wrong — so it demotes to review rather than being thrown
  away.
* **SEO score.** A post that would rank poorly should not go out unreviewed even
  when the model is confident it is accurate. Confidence is about truth; the
  score is about whether anyone will find it.
* **SEO errors.** Separate from the score on purpose. The score asks whether the
  piece is good enough and answers in points a strong piece can absorb; an
  ``error`` from :func:`app.services.seo.audit` asks whether it is broken. A
  piece with no meta description scored exactly the threshold and went out.

They all demote to review rather than throwing the piece away, and so does the
last thing that can stop an unreviewed publish: having nowhere to send it. See
:func:`publishable_destinations`, which drops a destination the owner never
connected instead of queueing a publication that can only fail.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType, unique_content_slug
from app.models.mixins import as_aware, utcnow
from app.models.project import AutopilotMode, Project
from app.models.publication import Platform, Publication
from app.models.webhook import WebhookEvent
from app.services import (
    ai,
    content_generator,
    dedup,
    factcheck,
    github_client,
    link_check,
    publishers,
    publishing_service,
    quality,
    seo,
    webhook_payloads,
    webhooks,
)
from app.services.signals import TriggerSignal

logger = logging.getLogger(__name__)

#: What ``RoutedContent.status`` can be. Strings rather than an enum because they
#: are a task return value read by a human in a log, not a stored column.
QUEUED_FOR_REVIEW = "queued_for_review"
AUTO_PUBLISHED = "auto_published"


class GenerationUnavailable(RuntimeError):
    """No LLM provider could be reached, so nothing was written.

    Distinct from a generation that produced a poor piece: here the model was
    never asked, because every provider was rate-limited or its circuit was
    open. Raised only when the caller passes ``defer_on_outage=True`` to say it
    can ask again — see :func:`generate_and_route`.

    The free-tier quotas this runs on reset daily, so a caller that *can* retry
    should do nothing at all and let the next sweep write the piece properly.
    The alternative, which this replaces for those callers, was to store the
    template: it burned the day's content budget, moved the watermark past the
    commits, and left a stub in the review queue that no later scan would ever
    supersede — so a single exhausted quota permanently cost those commits their
    post.
    """


@dataclass
class RoutedContent:
    """A generated piece and what was decided about it."""

    content: Content
    status: str
    confidence: float
    seo_score: int
    #: The prose score — readability and code density folded in with the SEO
    #: envelope. ``None`` only for callers built before the gate existed; the
    #: pipeline always sets it. See :mod:`app.services.quality`.
    quality_score: int | None = None
    dead_links: list[str] = field(default_factory=list)
    #: ``error``-level SEO issues, which hold a piece back on their own however
    #: it scored — see :func:`app.services.seo.blocking_issues`.
    seo_errors: list[str] = field(default_factory=list)
    #: What the model spliced into the copy, which holds a piece back the same
    #: way: runs from a script Herald never writes in
    #: (:func:`app.services.ai.stray_script_runs`) followed by words carrying a
    #: letter that is not theirs (:func:`app.services.ai.stray_letter_splices`).
    garbled_runs: list[str] = field(default_factory=list)
    #: Product and feature names the copy claims that nothing on file supports
    #: — see :mod:`app.services.factcheck`. Holds a piece back the same way the
    #: three above do.
    unsupported_names: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    is_fallback: bool = False

    @property
    def auto_published(self) -> bool:
        """Whether the pipeline published this itself, with nobody in the loop."""
        return self.status == AUTO_PUBLISHED

    def summary(self) -> dict[str, Any]:
        """The dict a Celery task returns for this outcome."""
        body: dict[str, Any] = {
            "status": self.status,
            "content_id": self.content.id,
            "confidence": self.confidence,
            "seo_score": self.seo_score,
        }
        if self.quality_score is not None:
            body["quality_score"] = self.quality_score
        if self.dead_links:
            body["dead_links"] = self.dead_links
        if self.seo_errors:
            body["seo_errors"] = self.seo_errors
        if self.garbled_runs:
            body["garbled_runs"] = self.garbled_runs
        if self.unsupported_names:
            body["unsupported_names"] = self.unsupported_names
        if self.platforms:
            body["platforms"] = self.platforms
        return body


def _unsupported_claims(
    project: Project,
    text: str,
    *,
    activity: Any = None,
    signal: TriggerSignal | None = None,
) -> list[factcheck.Claim]:
    """Product names in *text* that nothing Herald knows about *project* supports.

    Two passes, and the second one is why this is a function rather than two
    lines inline. The first grounds the copy against what is already in the
    database — the brief, the keywords, the commits or feed entries that
    prompted the piece — which costs nothing and settles the great majority of
    pieces, because the great majority of pieces name nothing surprising.

    Only a piece that *fails* that pass is worth a network call, and then the
    README is the right thing to fetch: it is the project describing itself, in
    its own vocabulary, including the feature names that live nowhere in
    Herald's columns. A piece naming a real module that the brief never
    mentioned is exactly the false positive this removes, and it is paid for
    once, by the pieces that earned it.

    Never raises. A GitHub outage, a rate limit, a repo the token cannot see —
    all of them mean "no README to check against", which leaves the first pass's
    answer standing. Failing a generation because a *validator's* optional input
    was unavailable would be the gate doing more harm than the thing it guards.
    """
    known = factcheck.project_vocabulary(project, activity=activity, signal=signal)
    claims = factcheck.unsupported_names(text, known)
    if not claims or not settings.factcheck_readme_enabled:
        return claims

    full_name = project.repo_full_name
    if not full_name:
        return claims
    try:
        readme = github_client.fetch_readme(full_name)
    except github_client.GitHubError as exc:
        logger.info("no README to fact-check project %s against: %s", project.id, exc)
        return claims
    if not readme:
        return claims
    return factcheck.unsupported_names(text, known | factcheck.vocabulary(readme))


def generate_and_route(
    db: Session,
    project: Project,
    *,
    content_type: ContentType,
    signal: TriggerSignal | None = None,
    activity: Any = None,
    instructions: str = "",
    source: dict[str, Any],
    defer_on_outage: bool = False,
) -> RoutedContent:
    """Write one piece for *project* and route it. Commits.

    *source* is the provenance dict stored on the content row; this adds the
    quality-gate results to it so a reviewer can see why a piece was held back
    without reading the logs.

    *defer_on_outage* decides what happens when no provider answers at all, and
    the right value follows from one question: **can this exact signal arrive
    again, unchanged, on the next sweep?** Only one caller can say yes — the
    project-level repo scan in :mod:`app.tasks.autopilot_tasks`, whose watermark
    is a column it has not written yet. It passes ``True``, nothing is stored,
    and the next scan reads the same commits and writes about them properly.

    Every other caller keeps the default and takes the template, which is worse
    than a post and much better than silence. An inbound webhook cannot re-read
    anything: the request is gone once it returns. A *trigger* — RSS, GitHub or
    schedule — cannot either, and the reason is worth stating because the source
    behind it plainly could: by the time generation starts, ``triggers.fire``
    has already committed a ``TriggerEvent`` carrying the signal's dedupe key,
    and ``_check_rss``/``_check_github`` have already committed the ``seen_ids``
    or ``last_sha`` watermark. Deferring there would leave a signal that the
    next poll dedupes away and never writes at all, which is strictly worse than
    a stub in the review queue with ``confidence=0.0`` on it.

    Raises :class:`GenerationUnavailable` on that path, having written nothing.
    """
    generated = content_generator.generate(
        project,
        content_type,
        activity=activity,
        signal=signal,
        instructions=instructions,
    )

    # Before anything is added to the session: a total provider outage must
    # leave no trace, or the retry it is asking for cannot happen cleanly.
    if defer_on_outage and (
        generated.fallback_reason == content_generator.FALLBACK_NO_PROVIDER
    ):
        logger.warning(
            "no LLM provider reachable — wrote nothing for project %s (%s)",
            project.id,
            content_type.value,
        )
        raise GenerationUnavailable(
            f"No LLM provider was reachable while writing for project {project.id}."
        )

    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    confident = generated.confidence >= settings.autopilot_auto_publish_confidence
    destinations = publishable_destinations(project)
    auto = bool(mode == AutopilotMode.AUTO and confident and destinations.usable)

    dead_links: list[str] = []
    if auto and settings.link_check_enabled:
        dead_links = [
            s.url
            for s in link_check.broken(link_check.check_body(generated.body_markdown))
        ]
        if dead_links:
            auto = False
            logger.info(
                "held %r back from auto-publish: %d dead link(s): %s",
                generated.title,
                len(dead_links),
                ", ".join(dead_links),
            )

    slug = unique_content_slug(db, project.id, generated.title)
    seo_fields = {
        "title": generated.title,
        "body_markdown": generated.body_markdown,
        "meta_description": generated.meta_description,
        "keywords": generated.keywords,
        "focus_keyword": generated.focus_keyword,
        "slug": slug,
        "cover_image_url": None,  # an automated piece rarely has one
    }
    score = seo.seo_score(**seo_fields)
    if auto and score < seo.SEO_SCORE_THRESHOLD:
        auto = False
        logger.info(
            "held %r back from auto-publish: SEO score %d < %d",
            generated.title,
            score,
            seo.SEO_SCORE_THRESHOLD,
        )

    # A second gate, and not a redundant one: the score asks whether the piece
    # is good enough, and this asks whether it is broken. The two came apart at
    # exactly the wrong place — a piece with no meta description loses fifteen
    # points for it and five for the cover image an automated piece never has,
    # scoring 70 against a `< 70` threshold. It passed by a rounding of the
    # deductions, and Herald auto-published, under the user's name, a post whose
    # own SEO panel led with "No meta description". See `seo.blocking_issues`.
    #
    # Asked even when `auto` is already false so the reason is banked on the row
    # either way: a reviewer looking at a held-back piece should see every
    # reason it was held, not the first one that fired.
    seo_errors = [issue.message for issue in seo.blocking_issues(**seo_fields)]
    if auto and seo_errors:
        auto = False
        logger.info(
            "held %r back from auto-publish: %d SEO error(s): %s",
            generated.title,
            len(seo_errors),
            "; ".join(seo_errors),
        )

    # A third gate, on the one thing neither of the others reads: the words. The
    # SEO score measures structure and the blocking issues measure completeness,
    # and a body with a Cyrillic noun wedged mid-sentence is structurally perfect
    # and complete. Six pieces auto-published to Dev.to and Bluesky under the
    # user's name carrying runs like `front<CJK> end` before anything looked.
    #
    # Banked on the row even when `auto` is already false, for the same reason
    # the SEO errors are: the reviewer should see every reason, and these runs
    # are the only one that tells them *where* to edit.
    #
    # Both halves of it. `stray_script_runs` reads the scripts Herald never
    # writes in; `stray_letter_splices` reads the ranges that gate exempts on
    # purpose — Greek, and the Latin supplements that make `résumé` legal — where
    # the same slip produces a word rather than a run. Two of the pieces that
    # went out carried only the second kind.
    # Every field the model wrote, not just the prose ones. `tags`, `keywords`
    # and `focus_keyword` came out of the same sampler as the body and reach a
    # published artefact just as directly — Dev.to renders `tags` as the post's
    # public tags, and `keywords`/`focus_keyword` drive the meta the SEO panel
    # reports on — but they sat outside every gate below, so a slip that would
    # have held the piece back from the body went out unread from a tag.
    #
    # The tag is the worse half of it, because the corruption does not survive to
    # be seen. `publishers.formatting.normalize_tags` strips a tag to `[a-z0-9]`
    # for the platforms that demand it, so `wörkflow` publishes as `wrkflow` and
    # a Cyrillic `аutomation` as `utomation`: not a visible glitch a reader
    # discounts, but a plausible, permanent, wrong tag. Reading the tags *here*,
    # before that normalisation, is the only place the evidence still exists.
    checked = "\n".join(
        part
        for part in (
            generated.title,
            generated.body_markdown,
            generated.excerpt,
            generated.meta_description,
            " ".join(generated.tags or []),
            " ".join(generated.keywords or []),
            generated.focus_keyword,
        )
        if part
    )
    garbled = ai.stray_script_runs(checked) + ai.stray_letter_splices(checked)
    if auto and garbled:
        auto = False
        logger.info(
            "held %r back from auto-publish: %d garbled run(s): %s",
            generated.title,
            len(garbled),
            ", ".join(garbled),
        )

    # A fourth gate, on the claims rather than the characters. The three above
    # all read the piece as an artefact — is it structurally sound, is it
    # complete, is it spelled in one script — and a model that invents a product
    # satisfies every one of them. `## Integration with Wird` scored fine, had
    # no dead links, and is ASCII from end to end.
    #
    # Banked on the row whether or not it holds the piece back, for the same
    # reason as the gates above, and computed even when the gate is switched
    # off: a name nothing supports is the single most useful thing a reviewer
    # can be handed about an automated piece, and the switch is about whether
    # Herald *acts* on it, not about whether the reviewer gets to see it.
    unsupported = _unsupported_claims(project, checked, activity=activity, signal=signal)
    if auto and unsupported and settings.factcheck_enabled:
        auto = False
        logger.info(
            "held %r back from auto-publish: %d unsupported name(s): %s",
            generated.title,
            len(unsupported),
            ", ".join(str(claim) for claim in unsupported),
        )

    # A fifth gate, and the only one that reads the piece against the *other*
    # pieces rather than against itself. Everything above asks whether this
    # article is sound; this asks whether it is the second copy of one Herald
    # already wrote for the same project.
    #
    # It runs here, after the generation, because for the autopilot path the
    # thing being judged is the title and the title is what the generation
    # produced — there is nothing to compare before the model has answered. The
    # commit-level check that *does* run first lives in
    # :func:`app.tasks.autopilot_tasks._act_on`; the two are complementary, and
    # :mod:`app.services.dedup` explains why neither is sufficient alone.
    #
    # Held back rather than discarded, like every gate above it. The piece is
    # written and paid for, a near-duplicate is a judgement rather than a fact,
    # and a reviewer looking at the two side by side is the right resolution —
    # whereas an auto-publish puts the twin on Dev.to next to its original,
    # where it cannot be taken back.
    duplicate_of = dedup.duplicate_of(db, project.id, title=generated.title)
    if auto and duplicate_of is not None:
        auto = False
        logger.info(
            "held %r back from auto-publish: restates content %s",
            generated.title,
            duplicate_of,
        )

    # A sixth gate, on the one thing none of the five above reads: the prose.
    #
    # Every gate so far measures the piece as an artefact or as a claim. The SEO
    # score reads the envelope, `blocking_issues` reads it for completeness, the
    # garble check reads the characters, the factcheck reads the names, and the
    # dedup check reads it against its siblings. A piece can pass all five and
    # still be four hundred words of subordinate clauses at a reading ease a
    # standards document would be embarrassed by, or six fenced blocks with a
    # sentence of transition between them — the two things a model writes when
    # the brief is thin. See :mod:`app.services.quality`.
    #
    # Herald already measures exactly this, and already refuses on it: it is the
    # DRAFT→REVIEW floor a human hits when they submit a piece by hand. So the
    # gate a *person* has to clear to ask for a reviewer was stricter than the
    # one Herald cleared to publish under that person's name unread. This closes
    # that, at its own threshold — see
    # ``settings.autopilot_auto_publish_min_quality`` for why the two numbers
    # are separate and why this one is higher.
    #
    # Scored from `seo_fields`, which is the same dict `seo.seo_score` and
    # `seo.blocking_issues` were handed above, so the piece cannot score one
    # envelope here and another there. `quality.report` takes those keyword
    # arguments precisely so there is one spelling of "what a piece is".
    #
    # Banked on the row whether or not it holds the piece back, like the four
    # gates before it: a reviewer looking at a held piece should see every
    # reason it was held, and this is the one that says *read it again*.
    quality_report = quality.report(**seo_fields)
    quality_floor = settings.autopilot_auto_publish_min_quality
    if auto and quality_floor > 0 and quality_report.score < quality_floor:
        auto = False
        logger.info(
            "held %r back from auto-publish: quality score %d < %d "
            "(reading ease %s, code ratio %.2f)",
            generated.title,
            quality_report.score,
            quality_floor,
            quality_report.readability.reading_ease,
            quality_report.code_ratio,
        )

    content = content_generator.content_from_generated(
        db,
        project_id=project.id,
        content_type=content_type,
        generated=generated,
        status=ContentStatus.APPROVED if auto else ContentStatus.REVIEW,
        source={
            **source,
            "fallback": generated.is_fallback,
            "dead_links": dead_links,
            "seo_score": score,
            "seo_errors": seo_errors,
            "garbled_runs": garbled,
            # The score and the two components behind it, not just the score. A
            # reviewer told "quality 54" can do nothing with it; told the
            # reading ease is 21 they know to shorten the sentences, and told
            # the code ratio is 0.7 they know to write the glue.
            "quality_score": quality_report.score,
            "reading_ease": quality_report.readability.reading_ease,
            "code_ratio": round(quality_report.code_ratio, 3),
            # Banked whether or not it held the piece back, like the gate
            # results above: "this restates #41" is the single most useful
            # sentence a reviewer can be handed about a piece that looks
            # familiar, and it is not reconstructible later — the window it was
            # judged over moves.
            "duplicate_of": duplicate_of,
            # The name *and* the sentence it appears in. A reviewer judging
            # "Wird" cannot do it from the word alone — the question is what the
            # piece claims about it, and that is in the surrounding clause.
            "unsupported_names": [
                {"name": claim.name, "rule": claim.rule, "context": claim.context}
                for claim in unsupported
            ],
            # Alongside the gate results and for the same reason: a reviewer
            # looking at a piece that was meant to publish itself should be able
            # to see why it did not without reading the logs.
            "unconnected_platforms": destinations.unconnected,
        },
    )
    db.add(content)
    db.flush()

    if not auto:
        db.commit()
        # The review queue is only a queue if somebody knows it has something in
        # it. This is the one moment an automated piece needs a human and cannot
        # ask for one through a UI nobody is looking at.
        webhooks.emit(
            db,
            user_id=project.user_id,
            event=WebhookEvent.REVIEW_PENDING,
            data={
                "content": webhook_payloads.content_payload(content),
                "confidence": generated.confidence,
                "seo_score": score,
                "seo_errors": seo_errors,
                "dead_links": dead_links,
                "unsupported_names": [claim.name for claim in unsupported],
                "review_url": f"{settings.frontend_url.rstrip('/')}/content/{content.id}",
            },
        )
        return RoutedContent(
            content=content,
            status=QUEUED_FOR_REVIEW,
            confidence=generated.confidence,
            seo_score=score,
            quality_score=quality_report.score,
            dead_links=dead_links,
            seo_errors=seo_errors,
            garbled_runs=garbled,
            unsupported_names=[claim.name for claim in unsupported],
            is_fallback=generated.is_fallback,
        )

    publications = publishing_service.queue(db, content, destinations.usable)
    db.commit()
    # Only the rows whose time has come. A project with a canonical platform and
    # more than one autopilot destination has its syndicated copies parked behind
    # the original by ``publishing_service._syndication_schedule``; the beat sweep
    # picks those up when the delay is up.
    for publication in publications:
        if publication.scheduled_for is None:
            publish_now(publication.id)

    return RoutedContent(
        content=content,
        status=AUTO_PUBLISHED,
        confidence=generated.confidence,
        seo_score=score,
        quality_score=quality_report.score,
        platforms=[p.platform.value for p in publications],
        is_fallback=generated.is_fallback,
    )


def release_approved(db: Session, content: Content) -> list[Publication]:
    """Queue an approved piece for its project's autopilot destinations.

    This is what makes the review queue a *queue* rather than a filing cabinet.
    A project on ``auto`` publishes what the autopilot writes without asking;
    the two quality gates above (dead links, SEO score) divert a piece to review
    instead, and a human clicking Approve is that piece clearing the gate. Before
    this, approving set a column and stopped: nothing queued a publication and no
    sweep looked for approved content, so five pieces sat ``approved`` on the box
    for days while the beat swept a publications table that had no rows for them.

    Deliberately narrow. It fires only when:

    * the piece is ``approved`` — a draft is not waiting on anything, and a
      published or failed one is finished;
    * nothing has been queued for it yet. A publication row of any status means a
      destination was already chosen, by a human or by an earlier pass, and this
      must not add to it or re-arm a row somebody cancelled;
    * the project is active, its owner's account is active, and it is on
      ``auto`` with destinations configured. ``off`` and ``draft`` mean "do not
      publish without me", and approving is not the same as saying where to.

    A piece the user has already dated keeps that date: ``content.scheduled_for``
    in the future is a decision about when this goes out, and approving it is
    saying yes to the piece, not moving it to now.

    Returns the publications it queued — empty whenever any of those does not
    hold, which is the common case. Commits when it queues, and dispatches the
    rows whose time has come.
    """
    if content.status != ContentStatus.APPROVED:
        return []
    if content.publications:
        return []

    project = content.project
    if project is None or not project.is_active:
        return []
    if project.user is None or not project.user.is_active:
        return []
    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )
    if mode != AutopilotMode.AUTO:
        return []

    platforms = publishable_destinations(project).usable
    if not platforms:
        return []

    when = as_aware(content.scheduled_for) if content.scheduled_for else None
    if when is not None and when <= utcnow():
        # A date that has already passed is not a schedule any more. Queue it
        # for now rather than for the past, which reads the same to the sweep
        # and worse in the UI.
        when = None

    publications = publishing_service.queue(db, content, platforms, scheduled_for=when)
    db.commit()
    for publication in publications:
        if publication.scheduled_for is None:
            publish_now(publication.id)
    logger.info(
        "released approved content %s to %s",
        content.id,
        ", ".join(p.platform.value for p in publications),
    )
    return publications


@dataclass(frozen=True)
class _Destinations:
    """Where the autopilot may publish, and what it had to drop to get there."""

    usable: list[Platform]
    #: Named destinations the owner has no live connection for, as platform
    #: values. Recorded on the piece so a reviewer is told why it is in front of
    #: them; see :func:`generate_and_route`.
    unconnected: list[str]


def publishable_destinations(project: Project) -> _Destinations:
    """The autopilot destinations Herald can actually post to.

    ``autopilot_platforms`` is a plain JSON column. Values written before the
    schema validators existed can name a platform with no finished adapter, and
    rows written through the ORM's enum machinery spell the name in upper case
    (see :class:`app.models.publication.Platform`). Both are read here rather
    than trusted: an unknown string would raise out of a beat sweep, and an
    unfinished adapter fails the publication terminally, which drives the piece
    to ``failed`` instead of leaving it approved.

    A platform the owner has never connected is dropped for that second reason,
    which is the same reason and the same outcome. ``_credentials_for`` raises
    ``NotConnected``, ``execute`` treats it as terminal — correctly, since no
    amount of retrying connects an account — and ``sync_content_status`` then
    walks a piece whose every publication is terminal to ``failed``. So a
    project set to ``auto`` with one destination it had never connected wrote a
    piece, approved it, queued it, burned it on the first attempt, and left the
    work in ``failed`` with nothing to be done but connect the account and retry
    by hand. The piece is worth more than that: dropped here, an unconnected
    destination costs the *publish* rather than the post, and if it was the only
    one then nothing is publishable, ``generate_and_route`` declines to
    auto-publish, and the piece goes to a human instead of to the bin.

    The manual publish path already refuses an unconnected platform outright
    (``routers.content._queue_publish``), with a 400 that names it. This is the
    autopilot's version of the same check — there is nobody to show a 400 to, so
    it drops the destination and says so on the piece.
    """
    connected = set(project.user.connected_platforms) if project.user else set()
    out: list[Platform] = []
    unconnected: list[str] = []
    for raw in project.autopilot_platforms or []:
        try:
            platform = raw if isinstance(raw, Platform) else Platform(raw)
        except ValueError:
            logger.warning(
                "project %s names an unknown autopilot destination %r", project.id, raw
            )
            continue
        if not publishers.get_adapter(platform).implemented:
            logger.warning(
                "project %s names %s, which has no finished adapter",
                project.id,
                platform.value,
            )
            continue
        if platform.value not in connected:
            if platform.value not in unconnected:
                unconnected.append(platform.value)
            logger.warning(
                "project %s publishes to %s, which its owner has not connected — "
                "skipping the destination rather than failing the piece",
                project.id,
                platform.value,
            )
            continue
        if platform not in out:
            out.append(platform)
    return _Destinations(usable=out, unconnected=unconnected)


def publish_now(publication_id: int) -> None:
    """Hand a publication to a worker, or do it here if the broker is down."""
    from app.tasks import publish_tasks

    if settings.celery_enabled:
        try:
            publish_tasks.publish_one.delay(publication_id)
            return
        except Exception as exc:  # pragma: no cover - broker down
            logger.warning("dispatch failed, publishing inline: %s", exc)
    publish_tasks.publish_one(publication_id)


__all__ = [
    "AUTO_PUBLISHED",
    "QUEUED_FOR_REVIEW",
    "GenerationUnavailable",
    "RoutedContent",
    "generate_and_route",
    "publish_now",
    "publishable_destinations",
    "release_approved",
]
