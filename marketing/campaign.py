#!/usr/bin/env python3
"""Drive a Pulse content campaign from a declarative plan.

A campaign file describes the projects that should exist and the articles that
should be in them. This runner makes Pulse match that description. It is
**idempotent**: running it twice creates nothing twice, and running it after
editing an article body updates the piece rather than duplicating it.

    export HERALD_EMAIL=... HERALD_PASSWORD=...

    ./campaign.py plan     campaigns/products-2026-q3.json
    ./campaign.py sync     campaigns/products-2026-q3.json
    ./campaign.py links    campaigns/products-2026-q3.json
    ./campaign.py schedule campaigns/products-2026-q3.json --start 2026-08-12T09:00 --every 3
    ./campaign.py status   campaigns/products-2026-q3.json

``sync`` never publishes and never schedules. Content lands in the review
queue, which is where a human decides whether it is worth putting out —
see the note in the README on why that decision is not automated.

``schedule`` is the only subcommand that arranges for anything to leave the
building, and it refuses to run against a platform that is not connected.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from herald_client import DEFAULT_BASE_URL, HeraldClient, HeraldError  # noqa: E402

#: Project fields the runner will correct on a project that already exists.
#:
#: Deliberately narrow. The full spec is sent when a project is *created*, but
#: an existing project's description, keywords, tone and audience are the
#: user's own copy — a campaign that quietly rewrote them would be destroying
#: curated data to make a plan file true. What is listed here is only the
#: machinery that decides how content gets published, which is the campaign's
#: legitimate business:
#:
#: * ``canonical_platform`` / ``auto_canonical`` — which destination counts as
#:   the original, so syndicated copies link back to it instead of competing
#:   with it for the same query.
#: * ``live_url`` — the fallback share link and the host UTM tagging applies to.
#: * ``utm_*`` — attribution for this campaign's outbound links.
#:
#: ``autopilot_mode`` is excluded on purpose: switching a project to unattended
#: publishing is a decision for a human, not a side effect of a content sync.
_PROJECT_SYNC_FIELDS = (
    "live_url",
    "auto_canonical",
    "canonical_platform",
    "utm_enabled",
    "utm_campaign",
)

#: Content fields compared against the plan on every sync — but only the ones an
#: article actually states. See :func:`_declared`.
_CONTENT_SYNC_FIELDS = (
    "body_markdown",
    "excerpt",
    "meta_description",
    "keywords",
    "tags",
    "focus_keyword",
    "content_type",
)


# --------------------------------------------------------------------------- #
# Plan loading                                                                 #
# --------------------------------------------------------------------------- #


class Plan:
    """A campaign file, with article bodies resolved from disk."""

    def __init__(self, path: Path):
        self.path = path
        self.root = path.parent
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.base_url: str = raw.get("base_url", DEFAULT_BASE_URL)
        #: Where ``body_file`` paths are resolved from, relative to the campaign
        #: file. Article bodies are shared across campaigns, so they live beside
        #: the campaigns directory rather than inside it.
        self.content_root = (path.parent / raw.get("content_root", "../content")).resolve()
        self.projects: list[dict] = raw.get("projects", [])
        self.series: dict[str, dict] = {s["key"]: s for s in raw.get("series", [])}
        self.articles: list[dict] = raw.get("articles", [])
        self._validate()

    def _validate(self) -> None:
        keys = {p["key"] for p in self.projects}
        seen: set[str] = set()
        for article in self.articles:
            key = article["key"]
            if key in seen:
                raise SystemExit(f"duplicate article key: {key}")
            seen.add(key)
            if article["project"] not in keys:
                raise SystemExit(
                    f"article {key!r} names project {article['project']!r}, "
                    "which the plan does not define"
                )
            series = article.get("series")
            if series and series not in self.series:
                raise SystemExit(f"article {key!r} names unknown series {series!r}")
            if not self.body_path(article).exists():
                raise SystemExit(f"article {key!r}: missing body {self.body_path(article)}")
        self._validate_series()

    def _validate_series(self) -> None:
        """Every series must number 1..n with nothing missing.

        :meth:`title` puts "(Part 2)" in a headline so a reader arriving from a
        search result knows there is a part 1. That promise runs both ways: a
        lone "(Part 1)" sends every reader looking for a part 2 that was never
        written, and a jump from 1 to 3 reads as a piece that got lost. Both are
        plan mistakes, and both are invisible until the post is public — which
        is exactly the kind of thing an offline check is for.
        """
        for key in self.series:
            parts = sorted(
                article["part"]
                for article in self.articles
                if article.get("series") == key and article.get("part")
            )
            if not parts:
                continue
            if parts != list(range(1, len(parts) + 1)):
                raise SystemExit(
                    f"series {key!r} is numbered {parts} — parts must run "
                    "1..n with no gaps and no repeats"
                )
            if len(parts) == 1:
                raise SystemExit(
                    f"series {key!r} has only part 1. A '(Part 1)' in the "
                    "headline promises a part 2 — add it, or drop the series "
                    "and part from the article."
                )

    def body_path(self, article: dict) -> Path:
        return (self.content_root / article["body_file"]).resolve()

    def body(self, article: dict) -> str:
        return self.body_path(article).read_text(encoding="utf-8").strip()

    def project(self, key: str) -> dict:
        return next(p for p in self.projects if p["key"] == key)

    def title(self, article: dict) -> str:
        """The headline, with a compact part marker for a series entry.

        A reader landing on part two from a search result has no way to know
        part one exists unless the title says so. Only the part number goes in,
        not the series name: Pulse flags a title over 60 characters because
        search results cut off around there, and " — 409A Valuation Guide
        (Part 2)" spends 30 of them on words nobody is searching for. The series
        name belongs in the body, where it costs nothing.
        """
        part = article.get("part")
        if not article.get("series") or not part:
            return article["title"]
        return f"{article['title']} (Part {part})"


# --------------------------------------------------------------------------- #
# Sync                                                                         #
# --------------------------------------------------------------------------- #


def sync_projects(client: HeraldClient, plan: Plan, *, dry_run: bool) -> dict[str, int]:
    """Ensure every project in the plan exists and matches. Returns key -> id."""
    existing = {p["name"].lower(): p for p in client.list_projects()}
    ids: dict[str, int] = {}

    for spec in plan.projects:
        payload = {k: v for k, v in spec.items() if k != "key"}
        current = existing.get(spec["name"].lower())

        if current is None:
            if dry_run:
                print(f"  + project {spec['name']} (would create)")
                ids[spec["key"]] = -1
                continue
            created = client.create_project(payload)
            ids[spec["key"]] = created["id"]
            print(f"  + project {spec['name']} created (id={created['id']})")
            continue

        ids[spec["key"]] = current["id"]
        drift = {
            field: payload[field]
            for field in _PROJECT_SYNC_FIELDS
            if field in payload and payload[field] != current.get(field)
        }
        if not drift:
            print(f"  = project {spec['name']} (id={current['id']})")
            continue
        if dry_run:
            print(f"  ~ project {spec['name']} would update: {', '.join(sorted(drift))}")
            continue
        client.update_project(current["id"], drift)
        print(f"  ~ project {spec['name']} updated: {', '.join(sorted(drift))}")

    return ids


def _campaign_key(plan: Plan, article: dict) -> str:
    """The stable handle this runner stores on the piece and matches against.

    Namespaced by campaign file so two campaigns can both contain an article
    keyed ``launch`` without colliding.
    """
    return f"{plan.path.stem}/{article['key']}"


def _declared(plan: Plan, article: dict) -> dict:
    """The content fields this article actually states.

    The distinction between "not stated" and "stated as empty" is the whole
    point, and it only bites on the *second* run. Pulse fills in a missing
    excerpt and meta description from the body at creation time. Sending
    ``excerpt: ""`` back for an article that never named one does not mean "no
    excerpt" — it means "replace the one Pulse wrote with nothing", which is
    an idempotent sync quietly destroying data it did not author.

    ``body_markdown`` is always declared: it comes from ``body_file``, which
    every article has, and it is the field the runner exists to keep in step.
    """
    declared = {
        field: article[field] for field in _CONTENT_SYNC_FIELDS if field in article
    }
    declared["body_markdown"] = plan.body(article)
    return declared


def _content_payload(plan: Plan, article: dict, project_id: int) -> dict:
    """The create body: everything declared, plus what Pulse needs up front."""
    return {
        "project_id": project_id,
        "content_type": article.get("content_type", "tutorial"),
        "title": plan.title(article),
        "campaign_key": _campaign_key(plan, article),
        **_declared(plan, article),
    }


def sync_articles(
    client: HeraldClient, plan: Plan, project_ids: dict[str, int], *, dry_run: bool
) -> dict[str, int]:
    """Create or update every article. Returns article key -> content id.

    Matched on the ``campaign_key`` stored in ``source`` at creation, falling
    back to the title for pieces created before this runner set one. The key
    matters because the title is the field an editing pass is most likely to
    change, and matching on it would orphan the row and create a duplicate.
    """
    ids: dict[str, int] = {}
    by_project: dict[int, list[dict]] = {}

    for article in plan.articles:
        project_id = project_ids[article["project"]]
        if project_id < 0:  # dry run against a project that does not exist yet
            print(f"  + {article['key']} (would create, project pending)")
            continue

        if project_id not in by_project:
            by_project[project_id] = client.list_content(project_id=project_id)
        existing = by_project[project_id]

        payload = _content_payload(plan, article, project_id)
        key = payload["campaign_key"]
        current = next(
            (c for c in existing if (c.get("source") or {}).get("campaign_key") == key),
            None,
        ) or next((c for c in existing if c["title"] == payload["title"]), None)
        want_status = article.get("status", "review")

        if current is None:
            if dry_run:
                words = len(payload["body_markdown"].split())
                print(f"  + {article['key']} (would create, {words} words)")
                continue
            created = client.create_content(payload)
            ids[article["key"]] = created["id"]
            if want_status != created["status"]:
                client.update_content(created["id"], {"status": want_status})
            print(
                f"  + {article['key']} created (id={created['id']}, "
                f"{created['word_count']} words, status={want_status})"
            )
            continue

        ids[article["key"]] = current["id"]

        if current["status"] == "published":
            print(f"  ! {article['key']} is published — leaving alone (id={current['id']})")
            continue

        # The list shape omits the body, so compare against the detail.
        detail = client.get_content(current["id"])
        declared = _declared(plan, article)
        drift = {
            field: value
            for field, value in declared.items()
            if value != detail.get(field)
        }
        if current["status"] != want_status:
            drift["status"] = want_status
        if not drift:
            print(f"  = {article['key']} (id={current['id']})")
            continue
        if dry_run:
            print(f"  ~ {article['key']} would update: {', '.join(sorted(drift))}")
            continue
        updated = client.update_content(current["id"], drift)
        print(f"  ~ {article['key']} updated: {', '.join(sorted(drift))}")
        _warn_if_not_converged(article["key"], drift, updated)

    return ids


def _warn_if_not_converged(key: str, sent: dict, stored: dict) -> None:
    """Say so when Pulse stored something other than what the plan asked for.

    Pulse normalises several of these fields — keywords are lowercased,
    deduplicated and capped at eight — so a plan can ask for something the API
    will never echo back. Nothing errors: the PATCH succeeds, the next run sees
    the same difference, and the runner reports an update forever while the
    stored value never moves. One line here turns a silent loop into a fixable
    complaint about the plan.
    """
    resisted = sorted(
        field
        for field, value in sent.items()
        if field != "status" and field in stored and stored[field] != value
    )
    if resisted:
        print(
            f"      note: {key}: Pulse normalised {', '.join(resisted)} — the "
            "plan and the stored value will differ on every run until the plan "
            "matches"
        )


# --------------------------------------------------------------------------- #
# Link check                                                                   #
# --------------------------------------------------------------------------- #


def check_links(client: HeraldClient, plan: Plan, content_ids: dict[str, int]) -> int:
    """Report dead links before they block a publish. Returns the broken count.

    Pulse runs this itself at publish time and refuses on a definitive 404, so
    finding out here is strictly better than finding out at 3am when the beat
    task tries to send the post.
    """
    broken = 0
    for article in plan.articles:
        content_id = content_ids.get(article["key"])
        if content_id is None:
            continue
        result = client.check_links(content_id)
        bad = [link for link in result["links"] if link["status"] == "broken"]
        broken += len(bad)
        flag = "!" if bad else "="
        print(f"  {flag} {article['key']}: {result['checked']} link(s), {len(bad)} broken")
        for link in bad:
            print(f"      {link['url']} ({link['http_status'] or 'unreachable'})")
    return broken


# --------------------------------------------------------------------------- #
# Schedule                                                                     #
# --------------------------------------------------------------------------- #


def schedule(
    client: HeraldClient,
    plan: Plan,
    content_ids: dict[str, int],
    *,
    start: datetime | None,
    every_days: int,
    optimize: bool,
    dry_run: bool,
) -> None:
    """Put the campaign on the calendar.

    Two modes. ``--optimize`` hands the timing to Pulse, which picks a slot
    per platform from its cadence table and staggers the cross-post. ``--start``
    plus ``--every`` lays the articles out on a fixed drumbeat, which is what
    you want when the cadence matters more than the hour of day.

    Either way this refuses to touch a platform with no live connection: a
    schedule that validates now and fails at 3am because nothing was ever
    connected is the worst of both worlds, and Pulse would reject it anyway.
    """
    connected = set(client.connected_platforms())
    wanted = {p for a in plan.articles for p in a.get("platforms", [])}
    missing = sorted(wanted - connected)
    if missing:
        raise SystemExit(
            f"Not connected to: {', '.join(missing)}.\n"
            "Connect them first (PUT /settings/connections, or Settings in the "
            "web UI) — Pulse rejects a publish to a platform with no live "
            "connection, so scheduling one would only fail later."
        )

    when = start
    for article in plan.articles:
        content_id = content_ids.get(article["key"])
        platforms = article.get("platforms", [])
        if content_id is None or not platforms:
            continue

        if optimize:
            label = "Pulse-chosen slots"
            kwargs: dict[str, Any] = {"optimize": True}
        else:
            assert when is not None
            label = when.isoformat()
            kwargs = {"scheduled_for": when.isoformat()}

        if dry_run:
            print(f"  → {article['key']}: {', '.join(platforms)} at {label}")
        else:
            publications = client.schedule_content(
                content_id, platforms=platforms, **kwargs
            )
            for publication in publications:
                print(
                    f"  → {article['key']}: {publication['platform']} "
                    f"{publication['status']} at {publication['scheduled_for']}"
                )

        if when is not None:
            when += timedelta(days=every_days)


# --------------------------------------------------------------------------- #
# Status                                                                       #
# --------------------------------------------------------------------------- #


def status(client: HeraldClient, plan: Plan) -> None:
    projects = {p["name"].lower(): p for p in client.list_projects()}
    # One listing per *project*, not one per article. A campaign is mostly
    # several articles against the same handful of projects, so fetching inside
    # the loop asked Pulse for the same list a dozen times to render one table.
    listings: dict[int, list[dict]] = {}
    print(f"{'article':<42} {'status':<10} publications")
    print("-" * 78)
    for article in plan.articles:
        spec = plan.project(article["project"])
        project = projects.get(spec["name"].lower())
        if project is None:
            print(f"{article['key']:<42} {'no project':<10}")
            continue
        key = _campaign_key(plan, article)
        if project["id"] not in listings:
            listings[project["id"]] = client.list_content(project_id=project["id"])
        listed = listings[project["id"]]
        match = next(
            (c for c in listed if (c.get("source") or {}).get("campaign_key") == key),
            None,
        ) or next((c for c in listed if c["title"] == plan.title(article)), None)
        if match is None:
            print(f"{article['key']:<42} {'absent':<10}")
            continue
        pubs = ", ".join(
            f"{p['platform']}:{p['status']}" for p in match.get("publications", [])
        )
        print(f"{article['key']:<42} {match['status']:<10} {pubs or '—'}")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _parse_start(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--start must be ISO 8601, got {value!r}") from exc


def _check_schedule_args(args: argparse.Namespace) -> None:
    """Refuse an unworkable schedule before anything has been written.

    All of this used to be caught late or not at all, and "late" is the
    problem: the check for a missing ``--start`` sat *after* the sync, so a
    mistyped flag created or updated a dozen articles and then bailed. Worse,
    ``--every 0`` was not checked anywhere — it stacked the whole campaign on
    one instant — and a negative value walked backwards into the past, where
    Pulse refuses each publish in turn, leaving half the campaign scheduled.
    """
    if args.optimize:
        if args.start is not None:
            raise SystemExit(
                "--optimize and --start are mutually exclusive: either Pulse "
                "picks the times or you do."
            )
        return

    if args.start is None:
        raise SystemExit("schedule needs --start (with --every) or --optimize")
    if args.every < 1:
        raise SystemExit(
            f"--every must be at least 1 day, got {args.every}. Zero would put "
            "the whole campaign out in the same instant."
        )

    # A naive --start is read as UTC by Pulse, so compare in UTC too rather
    # than against a local clock that would be wrong by the offset.
    start = args.start
    now = datetime.now(UTC)
    if start.tzinfo is None:
        now = now.replace(tzinfo=None)
    if start < now:
        raise SystemExit(
            f"--start {start.isoformat()} is in the past — Pulse refuses a "
            "publish dated backwards, so this would fail article by article."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive a Pulse content campaign from a declarative plan."
    )
    parser.add_argument(
        "command", choices=("plan", "sync", "links", "schedule", "status")
    )
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--base-url", default=None, help="overrides the campaign file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--start",
        type=_parse_start,
        help="ISO time of the first slot. Read as UTC unless it carries an offset.",
    )
    parser.add_argument("--every", type=int, default=3, help="days between articles")
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="let Pulse pick each platform's slot instead of --start/--every",
    )
    args = parser.parse_args(argv)

    # Argument checking before the plan is even loaded, and long before
    # anything is written: a bad flag must not cost a sync first.
    if args.command == "schedule":
        _check_schedule_args(args)

    plan = Plan(args.campaign)
    base_url = args.base_url or plan.base_url

    if args.command == "plan":
        # No network: this is the offline check that the file is coherent.
        print(f"campaign: {args.campaign}  →  {base_url}")
        print(f"  {len(plan.projects)} project(s), {len(plan.articles)} article(s)")
        for article in plan.articles:
            words = len(plan.body(article).split())
            print(
                f"    {article['key']:<42} {article['project']:<11} "
                f"{words:>5} words  [{', '.join(article.get('platforms', [])) or 'no platforms'}]"
            )
            print(f"      {plan.title(article)}")
        return 0

    with HeraldClient(base_url) as client:
        client.login_from_env()
        try:
            if args.command == "status":
                status(client, plan)
                return 0

            print(f"campaign: {args.campaign}  →  {base_url}")
            project_ids = sync_projects(client, plan, dry_run=args.dry_run)
            content_ids = sync_articles(
                client, plan, project_ids, dry_run=args.dry_run
            )

            if args.command == "links":
                broken = check_links(client, plan, content_ids)
                return 1 if broken else 0

            if args.command == "schedule":
                schedule(
                    client,
                    plan,
                    content_ids,
                    start=args.start,
                    every_days=args.every,
                    optimize=args.optimize,
                    dry_run=args.dry_run,
                )
        except HeraldError as exc:
            print(f"\nAPI error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
