#!/usr/bin/env python3
"""Drive a Herald content campaign from a declarative plan.

A campaign file describes the projects that should exist and the articles that
should be in them. This runner makes Herald match that description. It is
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
from datetime import datetime, timedelta
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

#: Content fields compared against the plan on every sync.
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
        not the series name: Herald flags a title over 60 characters because
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


def _content_payload(plan: Plan, article: dict, project_id: int) -> dict:
    return {
        "project_id": project_id,
        "content_type": article.get("content_type", "tutorial"),
        "title": plan.title(article),
        "body_markdown": plan.body(article),
        "excerpt": article.get("excerpt", ""),
        "meta_description": article.get("meta_description", ""),
        "keywords": article.get("keywords", []),
        "tags": article.get("tags", []),
        "focus_keyword": article.get("focus_keyword", ""),
        "campaign_key": _campaign_key(plan, article),
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
        drift = {
            field: payload[field]
            for field in _CONTENT_SYNC_FIELDS
            if payload[field] != detail.get(field)
        }
        if current["status"] != want_status:
            drift["status"] = want_status
        if not drift:
            print(f"  = {article['key']} (id={current['id']})")
            continue
        if dry_run:
            print(f"  ~ {article['key']} would update: {', '.join(sorted(drift))}")
            continue
        client.update_content(current["id"], drift)
        print(f"  ~ {article['key']} updated: {', '.join(sorted(drift))}")

    return ids


# --------------------------------------------------------------------------- #
# Link check                                                                   #
# --------------------------------------------------------------------------- #


def check_links(client: HeraldClient, plan: Plan, content_ids: dict[str, int]) -> int:
    """Report dead links before they block a publish. Returns the broken count.

    Herald runs this itself at publish time and refuses on a definitive 404, so
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

    Two modes. ``--optimize`` hands the timing to Herald, which picks a slot
    per platform from its cadence table and staggers the cross-post. ``--start``
    plus ``--every`` lays the articles out on a fixed drumbeat, which is what
    you want when the cadence matters more than the hour of day.

    Either way this refuses to touch a platform with no live connection: a
    schedule that validates now and fails at 3am because nothing was ever
    connected is the worst of both worlds, and Herald would reject it anyway.
    """
    connected = set(client.connected_platforms())
    wanted = {p for a in plan.articles for p in a.get("platforms", [])}
    missing = sorted(wanted - connected)
    if missing:
        raise SystemExit(
            f"Not connected to: {', '.join(missing)}.\n"
            "Connect them first (PUT /settings/connections, or Settings in the "
            "web UI) — Herald rejects a publish to a platform with no live "
            "connection, so scheduling one would only fail later."
        )

    when = start
    for article in plan.articles:
        content_id = content_ids.get(article["key"])
        platforms = article.get("platforms", [])
        if content_id is None or not platforms:
            continue

        if optimize:
            label = "Herald-chosen slots"
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
    print(f"{'article':<42} {'status':<10} publications")
    print("-" * 78)
    for article in plan.articles:
        spec = plan.project(article["project"])
        project = projects.get(spec["name"].lower())
        if project is None:
            print(f"{article['key']:<42} {'no project':<10}")
            continue
        key = _campaign_key(plan, article)
        listed = client.list_content(project_id=project["id"])
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive a Herald content campaign from a declarative plan."
    )
    parser.add_argument(
        "command", choices=("plan", "sync", "links", "schedule", "status")
    )
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--base-url", default=None, help="overrides the campaign file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", type=_parse_start, help="ISO time of the first slot")
    parser.add_argument("--every", type=int, default=3, help="days between articles")
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="let Herald pick each platform's slot instead of --start/--every",
    )
    args = parser.parse_args(argv)

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
                if not args.optimize and args.start is None:
                    raise SystemExit(
                        "schedule needs --start (with --every) or --optimize"
                    )
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
