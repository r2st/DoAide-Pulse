"""Auto-pilot: watch the repos, write when something ships.

The loop for one project is:

1. Ask GitHub what is new since the stored watermark.
2. Decide whether it is *worth* writing about (a release always is; loose
   commits only past a threshold).
3. Generate a piece, and bank an idea for later either way.
4. Route it — straight to publish if the project says ``auto`` and the model is
   confident, otherwise into the review queue.
5. Move the watermark, so the same commits never trigger twice.

Step 5 happens on almost every outcome, including when generation falls back to
a template. A watermark that only advances on success means one bad scan makes
every subsequent scan re-report the same backlog, and the daily cap then burns
itself on the same commits every day.

Two outcomes hold it instead, both because the commits were genuinely never
consumed and both because the condition ends on its own rather than needing a
human — so the backlog they defer is one a later sweep clears:

* A *total provider outage*, or GitHub rate-limiting the read. Nothing answered,
  so nothing was asked and nothing was written. Free-tier quotas reset daily.
* Commits that did not clear ``autopilot_commit_threshold``. The threshold means
  "not enough has happened **yet**", and it can only mean that if the commits it
  refuses are still there next time. Advancing past them turns it into "not
  enough happened this hour" — and against an hourly scan and a default of ten,
  a repo pushed at any human rate is refused every hour and loses those commits
  every hour, so the autopilot never writes about commits at all. Nothing is
  banked below the bar either: the same commits arrive again next hour, and an
  idea banked now would be re-banked every hour until they clear it.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.content import Content, ContentType
from app.models.mixins import elapsed_ms, utcnow
from app.models.project import AutopilotMode, Project, repo_full_name, scan_due
from app.models.trigger import Trigger, TriggerKind
from app.models.user import User
from app.services import content_generator, content_pipeline, dedup, github_client
from app.tasks.celery_app import task

logger = logging.getLogger(__name__)


def _pick_content_type(activity: github_client.RepoActivity) -> ContentType:
    """What kind of piece this change deserves.

    A release is an announcement — that is what a release *is*. A run of
    ordinary commits is a feature spotlight, because "here is what we've been
    building" reads better than an announcement with nothing to announce.
    """
    if activity.new_release:
        return ContentType.ANNOUNCEMENT
    return ContentType.FEATURE_SPOTLIGHT


#: Outcomes after which the watermark must stay where it is. See the module
#: docstring: these are the scans that read commits without consuming them, so
#: claiming to have seen them would throw them away.
_HOLDS_WATERMARK = frozenset({"below_threshold"})


def _worth_writing(activity: github_client.RepoActivity) -> bool:
    """Is there enough here to justify a post?

    A release always is. Loose commits need to clear a threshold: writing an
    announcement about three typo fixes is how an audience learns to ignore you.
    Below the bar the caller holds the watermark, so "not enough" is a verdict
    on the backlog so far rather than on this hour's slice of it.
    """
    if activity.new_release:
        return True
    return len(activity.new_commits) >= settings.autopilot_commit_threshold


def _daily_count(db: Session, project_id: int) -> int:
    since = utcnow() - timedelta(days=1)
    return (
        db.scalar(
            select(func.count(Content.id)).where(
                Content.project_id == project_id,
                Content.created_at >= since,
                Content.source["kind"].as_string() == "autopilot",
            )
        )
        or 0
    )


@task(
    name="app.tasks.autopilot_tasks.scan_project",
    soft_time_limit=180,
    time_limit=210,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def scan_project(project_id: int) -> dict:
    """Run the autopilot loop for one project. Never raises."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None or not project.is_active:
            return {"project_id": project_id, "status": "skipped"}
        # A deactivated account stops signing in but stopped nothing it had
        # already set running — its projects kept being scanned, written for and
        # published with its stored platform credentials. Checked here as well
        # as in ``scan_all_projects``'s query, because this task is also called
        # by id from elsewhere.
        if project.user is None or not project.user.is_active:
            return {"project_id": project_id, "status": "skipped"}

        full_name = project.repo_full_name
        if not full_name:
            return {"project_id": project_id, "status": "no_repo"}

        # Started here rather than at the top of the task: the three returns
        # above are cheap rejections that never touched GitHub, and timing them
        # would mix sub-millisecond no-ops into an average that exists to show
        # when the *network* half got slow.
        started = time.monotonic()

        try:
            activity = github_client.fetch_activity(
                full_name,
                since_sha=project.last_seen_commit_sha,
                since_tag=project.last_seen_release_tag,
            )
        except github_client.GitHubRateLimited as exc:
            # Do not move the watermark — we genuinely did not look.
            logger.warning("autopilot rate-limited on %s: %s", full_name, exc)
            return {"project_id": project_id, "status": "rate_limited"}
        except github_client.GitHubError as exc:
            logger.warning("autopilot could not read %s: %s", full_name, exc)
            project.record_scan(duration_ms=elapsed_ms(started))
            db.commit()
            return {"project_id": project_id, "status": "unreachable"}

        # First-ever scan: record where the repo is and write nothing. Otherwise
        # registering a five-year-old project produces a post about five years
        # of history.
        first_scan = project.last_seen_commit_sha is None
        try:
            result = _act_on(db, project, activity, first_scan=first_scan)
        except content_pipeline.GenerationUnavailable as exc:
            # Nothing was written and the watermark stays put, so the next sweep
            # sees these same commits and writes about them properly. Rolled
            # back explicitly: the ideas banked above this point belong to the
            # piece that was not written, and would otherwise be re-banked as
            # duplicates every scan until the quota returns.
            db.rollback()
            logger.warning("autopilot paused on project %s: %s", project_id, exc)
            return {"project_id": project_id, "status": "llm_unavailable"}

        if result.get("status") not in _HOLDS_WATERMARK:
            project.last_seen_commit_sha = activity.head_sha
            project.last_seen_release_tag = activity.latest_tag
        # Always: we did look, whatever we decided to do about it.
        project.record_scan(duration_ms=elapsed_ms(started))
        db.commit()
        return {"project_id": project_id, **result, "scan_ms": project.last_scan_duration_ms}
    except SoftTimeLimitExceeded:
        logger.warning("autopilot timed out on project %s", project_id)
        return {"project_id": project_id, "status": "timeout"}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("autopilot crashed on project %s: %s", project_id, exc)
        return {"project_id": project_id, "status": "error", "error": str(exc)}
    finally:
        db.close()


#: Shared with the machine API, which files ideas into the same table and has
#: to be held to the same cap — see :func:`app.services.content_generator.prune_ideas`.
_prune_ideas = content_generator.prune_ideas


def _act_on(
    db: Session, project: Project, activity: github_client.RepoActivity, *, first_scan: bool
) -> dict:
    """Decide and do, for one scanned project."""
    if first_scan:
        return {"status": "baselined", "commits": len(activity.new_commits)}
    if not activity.has_news:
        return {"status": "no_news"}

    mode = (
        project.autopilot_mode
        if isinstance(project.autopilot_mode, AutopilotMode)
        else AutopilotMode(project.autopilot_mode)
    )

    # Asked before anything is banked, because this is the one outcome the
    # caller holds the watermark for: these commits arrive again next scan, and
    # an idea banked from them now would be banked again from the same commits
    # every hour until they clear the bar. A project with the autopilot off is
    # not subject to it — it is here for its ideas, and there is no post for the
    # threshold to be protecting.
    if mode != AutopilotMode.OFF and not _worth_writing(activity):
        return {"status": "below_threshold", "commits": len(activity.new_commits)}

    # Bank an idea regardless of whether we write now: the calendar's
    # "suggested" column is fed from these, and an idea costs nothing to keep.
    #
    # Through `bank_ideas` rather than inserted here, because consecutive scans
    # of an active repo describe overlapping commits and say the same thing
    # twice — and during an LLM outage the fallback says *literally* the same
    # thing every hour, which `_prune_ideas` below then answers by deleting the
    # varied ideas instead. See the function's docstring.
    content_generator.bank_ideas(
        db,
        project.id,
        content_generator.suggest_ideas(project, activity=activity, limit=2),
        source={"kind": "autopilot", "commits": len(activity.new_commits)},
    )

    # Prune oldest unused ideas beyond the cap so the table stays bounded.
    _prune_ideas(db, project.id)

    if mode == AutopilotMode.OFF:
        return {"status": "ideas_only"}
    if _daily_count(db, project.id) >= settings.autopilot_daily_content_limit:
        return {"status": "daily_limit_reached"}

    # The one duplicate check that runs *before* the model is called, and the
    # only one that can: these commits are already in hand, and a stored piece
    # that names all of them is proof this scan is a repeat rather than a
    # judgement that it might be. Cheap enough to be worth asking every time —
    # one indexed window query against a generation.
    #
    # It fires where the two watermarks in this system overlap. A project can be
    # scanned by this sweep *and* watched by a `github` trigger with its own
    # `last_sha`, and the two advance independently; the trigger writing about a
    # push does not stop the sweep writing about the same push an hour later.
    # Nothing joined those up, and the piece it produced was not a near-duplicate
    # — it was the same commits, twice.
    #
    # Below the ideas, deliberately. An idea banked from these commits is still
    # worth having: `bank_ideas` has its own restatement check, so a genuine
    # repeat is dropped there, and the calendar's suggestions should not go
    # quiet because the article was already written.
    shas = dedup.commit_shas(activity)
    duplicate_id = dedup.duplicate_of(db, project.id, shas=shas)
    if duplicate_id is not None:
        logger.info(
            "project %s: scan covers commits already written up as content %s",
            project.id,
            duplicate_id,
        )
        return {"status": "duplicate", "duplicate_of": duplicate_id}

    routed = content_pipeline.generate_and_route(
        db,
        project,
        content_type=_pick_content_type(activity),
        activity=activity,
        source={
            "kind": "autopilot",
            "trigger": "release" if activity.new_release else "commits",
            "release_tag": activity.new_release.tag if activity.new_release else None,
            "commit_count": len(activity.new_commits),
            # What makes the check above possible next time. Written here rather
            # than inside the pipeline because this is the only caller that has
            # commits to record — a trigger firing on an RSS entry has none.
            dedup.SOURCE_COMMITS_KEY: shas,
        },
        # The scan can read the same commits again — see the module docstring.
        defer_on_outage=True,
    )
    return routed.summary()


@task(
    name="app.tasks.autopilot_tasks.scan_all_projects",
    soft_time_limit=120,
    time_limit=150,
    autoretry_for=(OperationalError, ConnectionError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
)
def scan_all_projects() -> dict:
    """Beat task: scan every active project that has a repo.

    A project with an active ``github`` trigger is skipped: the trigger keeps
    its own watermark and runs through the same pipeline, so scanning here as
    well would write about every push twice. The project-level scan is what a
    project gets until somebody sets a trigger up, not a competing mechanism.
    """
    db = SessionLocal()
    try:
        trigger_owned = select(Trigger.project_id).where(
            Trigger.kind == TriggerKind.GITHUB, Trigger.is_active.is_(True)
        )
        # The interval is filtered in Python rather than in the WHERE clause.
        # The comparison is ``last_scanned_at <= now - N hours`` where N is a
        # *column*, and column-driven interval arithmetic is spelled differently
        # in every dialect this runs on — Postgres in production, SQLite under
        # the tests. Three rows per project on a fleet of tens is not the read
        # worth spending a dialect branch on, and `Project.scan_due` is then one
        # testable answer both this and the projects page can quote.
        now = utcnow()
        candidates = db.execute(
            select(
                Project.id,
                Project.last_scanned_at,
                Project.autopilot_min_interval_hours,
                Project.repo_url,
            )
            .join(User, User.id == Project.user_id)
            .where(
                Project.is_active.is_(True),
                User.is_active.is_(True),
                Project.repo_url.is_not(None),
                Project.id.not_in(trigger_owned),
            )
        ).all()
        # ``repo_url IS NOT NULL`` is not the same question as "can this be
        # scanned", and the gap between them was a project dispatched on every
        # sweep for as long as it existed. A GitLab URL — or a typo, or the
        # repo's *web* page rather than its clone URL — passes the filter above,
        # is handed to a worker, and is turned away by ``scan_project`` at
        # ``repo_full_name``. That return is ``no_repo``, and it deliberately
        # does not call ``record_scan``: nothing looked at GitHub, so timing it
        # would mix a sub-millisecond no-op into an average that exists to show
        # when the network half got slow. The cost of that correctness is that
        # ``last_scanned_at`` stays NULL, and a project that has never scanned is
        # always due — so the interval never bit and the next tick did it again.
        #
        # Filtered here rather than made scannable, because there is no URL to
        # invent: the project needs its config fixed, and the warning below is
        # what says so.
        scannable = [
            (project_id, last_scanned_at, interval_hours)
            for project_id, last_scanned_at, interval_hours, repo_url in candidates
            if repo_full_name(repo_url) is not None
        ]
        ids = [
            project_id
            for project_id, last_scanned_at, interval_hours in scannable
            if scan_due(last_scanned_at, interval_hours, now)
        ]
        held = len(scannable) - len(ids)
        # A project set to draft or auto that nothing can ever scan. It will
        # never produce anything, and until this line the only evidence was that
        # absence. Named here so the fact is discoverable from the logs as well
        # as the projects page.
        #
        # Two ways to be in that state, and this used to report only the first.
        # A NULL ``repo_url`` never reaches the candidate query; a ``repo_url``
        # that is not a GitHub URL sails through it and is filtered out above.
        # The second is the worse of the two to leave unnamed, because it is the
        # one that *looks* configured — there is a URL in the box — and it was
        # also the one quietly costing a dispatch an hour. Both are the same
        # fault, ``Project._cannot_start`` already states it as one, and asking
        # ``repo_full_name`` here is the sweep's side of that single rule.
        any_trigger = select(Trigger.project_id).where(Trigger.is_active.is_(True))
        stranded = [
            name
            for name, repo_url in db.execute(
                select(Project.name, Project.repo_url)
                .join(User, User.id == Project.user_id)
                .where(
                    Project.is_active.is_(True),
                    User.is_active.is_(True),
                    Project.autopilot_mode != AutopilotMode.OFF,
                    Project.id.not_in(any_trigger),
                )
            ).all()
            if repo_full_name(repo_url) is None
        ]
    finally:
        db.close()

    if held:
        # At info, not warning: this is the interval doing exactly what it was
        # set to do. It is logged at all because "the autopilot has gone quiet"
        # and "the autopilot is on a two-day interval" look identical from
        # outside, and the first is a bug report.
        logger.info(
            "%d project(s) held back by their own scan interval this sweep", held
        )

    if stranded:
        logger.warning(
            "autopilot is on for %d project(s) with no repo and no trigger, "
            "so they can never scan: %s",
            len(stranded),
            ", ".join(stranded),
        )

    # Dispatch each project as a separate Celery task so they run in parallel
    # across workers instead of blocking a single task for the entire fleet.
    dispatched = 0
    failed = 0
    inline = 0
    # One warning per sweep rather than per project — see the same flag in
    # ``trigger_tasks.check_due_triggers``.
    broker_warned = False
    for project_id in ids:
        # Set from the inline branch's *return value*, not from an exception —
        # see the break at the bottom of the loop.
        out_of_time = False
        try:
            scan_project.delay(project_id)
        except SoftTimeLimitExceeded:
            # Must beat the broker fallback below, which would otherwise read
            # the timeout as a dead broker and answer it by scanning a repo and
            # calling a model inline, on a task with seconds left before the
            # hard limit. Undispatched projects are picked up by the next scan.
            logger.warning(
                "autopilot dispatch timed out after %d of %d project(s)",
                dispatched,
                len(ids),
            )
            break
        except Exception as exc:
            # Broker down — fall back to inline.
            #
            # This is the most expensive of the inline fallbacks: a scan reads a
            # repository over the network and calls a model. Doing the whole
            # fleet's worth of that on the beat thread, serially, is a decision
            # worth a line in the log — and the line that was here said
            # "dispatched N of N", which reads as though a worker took them.
            if not broker_warned:
                logger.warning("broker unavailable, scanning projects inline: %s", exc)
                broker_warned = True
            inline += 1
            try:
                out_of_time = scan_project(project_id).get("status") == "timeout"
            except Exception:
                # The same rule ``publish_due`` states outright: one project's
                # bad day must not end the pass. ``scan_project`` promises never
                # to raise, but the promise is made by a ``try`` it enters after
                # opening its session — so ``SessionLocal()`` and the ``close()``
                # in its ``finally`` are both outside it, and land here.
                #
                # Escaping the loop would not lose one project, it would drop
                # every later one in the fleet, and the next sweep selects the
                # same ids in the same order and dies in the same place.
                logger.exception("inline scan of project %s failed", project_id)
                failed += 1
                continue
        dispatched += 1
        if out_of_time:
            # The inline branch is serial and spends *this* task's budget, so it
            # is where the soft limit actually lands — but it never arrives here
            # as an exception. ``scan_project`` catches its own
            # ``SoftTimeLimitExceeded`` so as to leave the watermark where it is,
            # and Celery raises it once, so by the time control is back in this
            # loop the only trace left is the outcome it returned.
            #
            # Read as an ordinary result, the sweep carried on to the next
            # project — a repo read and a model call, started after the deadline
            # — and the one after that, until the hard limit killed the worker
            # outright. ``acks_late`` then redelivered the whole batch, and the
            # replacement worker began the fleet again from the top. The
            # remaining projects keep their watermarks; the next scan takes them.
            logger.warning(
                "autopilot timed out scanning inline after %d of %d project(s)",
                dispatched,
                len(ids),
            )
            break

    logger.info(
        "autopilot dispatched %d of %d project(s), %d inline, %d failed",
        dispatched,
        len(ids),
        inline,
        failed,
    )
    return {"scanned": len(ids), "dispatched": dispatched, "failed": failed}
