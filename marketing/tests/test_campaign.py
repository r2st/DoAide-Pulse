"""The campaign runner: plan validation, sync, scheduling and status.

The runner's one promise is idempotence — "running it twice creates nothing
twice, and running it after editing an article body updates the piece rather
than duplicating it". Most of what is here is that promise held to, because the
ways it breaks are all quiet: a second run that wipes a field Herald generated,
a drift report that never converges, a duplicate created because the title moved.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import campaign
import pytest
from conftest import PLAN, write_plan
from fake_herald import FakeHerald


@pytest.fixture
def plan(plan_path):
    return campaign.Plan(plan_path)


@pytest.fixture
def herald():
    return FakeHerald()


def sync(client, plan, *, dry_run=False):
    """Both halves of a sync, as ``main`` runs them."""
    project_ids = campaign.sync_projects(client, plan, dry_run=dry_run)
    return campaign.sync_articles(client, plan, project_ids, dry_run=dry_run)


# -- plan loading and validation ------------------------------------------- #


def test_a_plan_resolves_its_bodies_from_the_content_root(plan):
    assert len(plan.articles) == 3
    assert "Four problems, one name." in plan.body(plan.articles[0])


def test_an_article_naming_an_unknown_project_is_refused(plan_dir):
    broken = copy.deepcopy(PLAN)
    broken["articles"][0]["project"] = "nope"
    with pytest.raises(SystemExit, match="which the plan does not define"):
        campaign.Plan(write_plan(plan_dir, broken, "broken.json"))


def test_a_duplicate_article_key_is_refused(plan_dir):
    broken = copy.deepcopy(PLAN)
    broken["articles"][1]["key"] = broken["articles"][0]["key"]
    with pytest.raises(SystemExit, match="duplicate article key"):
        campaign.Plan(write_plan(plan_dir, broken, "broken.json"))


def test_a_missing_body_file_is_refused(plan_dir):
    broken = copy.deepcopy(PLAN)
    broken["articles"][0]["body_file"] = "never-written.md"
    with pytest.raises(SystemExit, match="missing body"):
        campaign.Plan(write_plan(plan_dir, broken, "broken.json"))


def test_a_series_with_only_part_one_is_refused(plan_dir):
    """A lone '(Part 1)' sends readers looking for a part 2 nobody wrote."""
    broken = copy.deepcopy(PLAN)
    broken["articles"] = [a for a in broken["articles"] if a["key"] != "matching"]
    with pytest.raises(SystemExit, match="only part 1"):
        campaign.Plan(write_plan(plan_dir, broken, "broken.json"))


def test_a_series_with_a_gap_in_its_numbering_is_refused(plan_dir):
    broken = copy.deepcopy(PLAN)
    broken["articles"][1]["part"] = 3
    with pytest.raises(SystemExit, match=r"numbered \[1, 3\]"):
        campaign.Plan(write_plan(plan_dir, broken, "broken.json"))


def test_a_series_nobody_wrote_for_yet_is_fine(plan_dir):
    """A declared series with no articles is intent, not an error."""
    later = copy.deepcopy(PLAN)
    later["series"].append({"key": "unwritten", "title": "Later"})
    campaign.Plan(write_plan(plan_dir, later, "later.json"))


def test_the_part_marker_goes_in_the_title_but_the_series_name_does_not(plan):
    """60 characters is the search-snippet budget; the series name is not worth it."""
    assert plan.title(plan.articles[0]) == "Where ITC Leaks (Part 1)"
    assert plan.title(plan.articles[1]) == "Matching With Tolerances (Part 2)"
    assert plan.title(plan.articles[2]) == "Herald Ships"


# -- project sync ----------------------------------------------------------- #


def test_projects_are_created_when_absent(herald, plan):
    ids = campaign.sync_projects(herald, plan, dry_run=False)
    assert set(ids) == {"gstbot", "herald"}
    assert {p["name"] for p in herald.projects} == {"GSTBot", "Herald"}


def test_a_second_sync_creates_nothing(herald, plan):
    campaign.sync_projects(herald, plan, dry_run=False)
    campaign.sync_projects(herald, plan, dry_run=False)
    assert len(herald.projects) == 2
    assert herald.count("create_project") == 2


def test_sync_corrects_publishing_machinery_but_not_the_users_copy(herald, plan):
    """The narrow field list is the point — see ``_PROJECT_SYNC_FIELDS``."""
    herald.add_project(
        name="GSTBot",
        description="A description the user rewrote by hand.",
        live_url="https://old.example.com",
        canonical_platform=None,
        auto_canonical=False,
        utm_enabled=False,
        utm_campaign="",
    )
    campaign.sync_projects(herald, plan, dry_run=False)

    stored = next(p for p in herald.projects if p["name"] == "GSTBot")
    assert stored["live_url"] == "https://gstbot.example.com"
    assert stored["canonical_platform"] == "devto"
    assert stored["utm_campaign"] == "gstbot-2026q3"
    # Untouched: the plan has no business rewriting curated copy.
    assert stored["description"] == "A description the user rewrote by hand."


def test_a_dry_run_writes_nothing(herald, plan):
    campaign.sync_projects(herald, plan, dry_run=True)
    assert herald.projects == []
    assert herald.count("create_project") == 0


# -- article sync ----------------------------------------------------------- #


def test_articles_are_created_with_the_campaign_key(herald, plan):
    ids = sync(herald, plan)
    assert set(ids) == {"reconciliation", "matching", "launch"}
    row = herald.by_title("Where ITC Leaks (Part 1)")
    assert row["source"]["campaign_key"] == "q3/reconciliation"
    assert row["status"] == "review"


def test_running_twice_creates_nothing_twice(herald, plan):
    first = sync(herald, plan)
    second = sync(herald, plan)
    assert first == second
    assert len(herald.content) == 3
    assert herald.count("create_content") == 3


def test_a_retitled_piece_is_matched_on_its_key_not_its_title(herald, plan):
    """The title is the field an edit is most likely to move."""
    ids = sync(herald, plan)
    herald._row(ids["reconciliation"])["title"] = "An Editor's Better Headline"

    again = sync(herald, plan)

    assert again["reconciliation"] == ids["reconciliation"]
    assert len(herald.content) == 3


def test_an_edited_body_updates_the_piece_rather_than_duplicating_it(herald, plan, plan_dir):
    ids = sync(herald, plan)
    (plan_dir / "content" / "reconciliation.md").write_text(
        "# Where ITC Leaks\n\nRewritten entirely.\n", encoding="utf-8"
    )

    sync(herald, campaign.Plan(plan.path))

    assert len(herald.content) == 3
    assert "Rewritten entirely." in herald._row(ids["reconciliation"])["body_markdown"]


def test_a_second_run_does_not_wipe_what_herald_generated(herald, plan):
    """The regression: an undeclared field was sent as "" on every run.

    ``launch`` names no excerpt and no meta description, so Herald writes both
    from the body. Sending them back empty does not mean "no excerpt" — it means
    "replace the one Herald wrote with nothing", which is an idempotent sync
    destroying data it did not author.
    """
    ids = sync(herald, plan)
    generated = herald._row(ids["launch"])["excerpt"]
    assert generated  # Herald filled it in

    sync(herald, plan)

    assert herald._row(ids["launch"])["excerpt"] == generated
    assert herald._row(ids["launch"])["meta_description"]


def test_a_declared_field_is_still_corrected(herald, plan):
    """Not sending undeclared fields must not stop the declared ones syncing."""
    ids = sync(herald, plan)
    herald._row(ids["reconciliation"])["excerpt"] = "Somebody pasted this in."

    sync(herald, plan)

    assert herald._row(ids["reconciliation"])["excerpt"] == (
        "Reconciliation is four problems wearing one name."
    )


def test_a_published_piece_is_left_alone(herald, plan):
    ids = sync(herald, plan)
    row = herald._row(ids["reconciliation"])
    row["status"] = "published"
    row["body_markdown"] = "What actually went out."
    herald.calls.clear()

    sync(herald, plan)

    assert row["body_markdown"] == "What actually went out."
    # Not merely un-updated: never even fetched for comparison.
    assert f"get_content:{ids['reconciliation']}" not in herald.calls
    assert f"update_content:{ids['reconciliation']}" not in herald.calls


def test_drift_herald_will_never_accept_is_reported_once_not_forever(
    herald, plan_dir, capsys
):
    """Nine keywords, and Herald stores eight. Silence here is an endless loop.

    Every run computes drift, PATCHes, gets the capped list back, and reports an
    update — for good. The runner cannot fix the plan, but it can say which
    field will never settle.
    """
    noisy = copy.deepcopy(PLAN)
    noisy["articles"][0]["keywords"] = [f"keyword number {n}" for n in range(9)]
    plan = campaign.Plan(write_plan(plan_dir, noisy, "noisy.json"))

    sync(herald, plan)
    capsys.readouterr()
    sync(herald, plan)

    out = capsys.readouterr().out
    assert "Herald normalised keywords" in out
    assert "reconciliation" in out


def test_a_clean_second_run_says_nothing_about_normalisation(herald, plan, capsys):
    sync(herald, plan)
    capsys.readouterr()
    sync(herald, plan)
    assert "normalised" not in capsys.readouterr().out


def test_article_listings_are_fetched_once_per_project_not_once_per_article(
    herald, plan
):
    sync(herald, plan)
    herald.calls.clear()
    sync(herald, plan)
    # Two projects, three articles.
    assert herald.count("list_content") == 2


# -- link check -------------------------------------------------------------- #


def test_check_links_counts_the_broken_ones(herald, plan, capsys):
    ids = sync(herald, plan)
    herald.link_results[ids["launch"]] = {
        "checked": 2,
        "broken_count": 1,
        "links": [
            {"url": "https://ok.example.com", "status": "ok", "http_status": 200},
            {"url": "https://gone.example.com", "status": "broken", "http_status": 404},
        ],
    }

    assert campaign.check_links(herald, plan, ids) == 1
    assert "https://gone.example.com (404)" in capsys.readouterr().out


def test_check_links_is_quiet_when_everything_resolves(herald, plan):
    ids = sync(herald, plan)
    assert campaign.check_links(herald, plan, ids) == 0


# -- schedule ---------------------------------------------------------------- #


def test_scheduling_refuses_a_platform_that_is_not_connected(herald, plan):
    herald.connected = ["devto"]  # the plan wants bluesky too
    ids = sync(herald, plan)

    with pytest.raises(SystemExit, match="Not connected to: bluesky"):
        campaign.schedule(
            herald, plan, ids,
            start=datetime(2026, 12, 1, 9, tzinfo=UTC),
            every_days=3, optimize=False, dry_run=False,
        )


def test_scheduling_lays_the_campaign_out_on_the_requested_drumbeat(herald, plan):
    ids = sync(herald, plan)
    start = datetime(2026, 12, 1, 9, tzinfo=UTC)

    campaign.schedule(
        herald, plan, ids, start=start, every_days=3, optimize=False, dry_run=False
    )

    when = [
        herald._row(ids[key])["publications"][0]["scheduled_for"]
        for key in ("reconciliation", "matching", "launch")
    ]
    assert when == [
        (start + timedelta(days=3 * n)).isoformat() for n in range(3)
    ]


def test_a_dry_run_schedules_nothing(herald, plan):
    ids = sync(herald, plan)
    campaign.schedule(
        herald, plan, ids,
        start=datetime(2026, 12, 1, 9, tzinfo=UTC),
        every_days=3, optimize=False, dry_run=True,
    )
    assert herald.count("schedule_content") == 0


# -- schedule argument checking ---------------------------------------------- #


def args(**overrides):
    import argparse

    base = {"optimize": False, "start": None, "every": 3}
    return argparse.Namespace(**{**base, **overrides})


def test_schedule_needs_a_start_or_optimize():
    with pytest.raises(SystemExit, match="needs --start"):
        campaign._check_schedule_args(args())


def test_optimize_and_start_together_are_refused():
    with pytest.raises(SystemExit, match="mutually exclusive"):
        campaign._check_schedule_args(
            args(optimize=True, start=datetime(2099, 1, 1, tzinfo=UTC))
        )


@pytest.mark.parametrize("every", [0, -1])
def test_a_cadence_of_zero_or_less_is_refused(every):
    """Zero stacked the whole campaign on one instant; negative walked backwards."""
    with pytest.raises(SystemExit, match="at least 1 day"):
        campaign._check_schedule_args(
            args(start=datetime(2099, 1, 1, tzinfo=UTC), every=every)
        )


def test_a_start_in_the_past_is_refused_before_anything_is_written():
    with pytest.raises(SystemExit, match="in the past"):
        campaign._check_schedule_args(args(start=datetime(2020, 1, 1, tzinfo=UTC)))


def test_a_naive_start_is_compared_against_utc():
    """Herald reads a naive timestamp as UTC, so the check must too."""
    naive_future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)
    campaign._check_schedule_args(args(start=naive_future))

    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    with pytest.raises(SystemExit, match="in the past"):
        campaign._check_schedule_args(args(start=naive_past))


def test_optimize_alone_needs_no_start():
    campaign._check_schedule_args(args(optimize=True))


# -- status ------------------------------------------------------------------ #


def test_status_reports_what_is_scheduled(herald, plan, capsys):
    ids = sync(herald, plan)
    campaign.schedule(
        herald, plan, ids,
        start=datetime(2026, 12, 1, 9, tzinfo=UTC),
        every_days=3, optimize=False, dry_run=False,
    )
    capsys.readouterr()

    campaign.status(herald, plan)

    out = capsys.readouterr().out
    assert "devto:scheduled" in out
    assert "bluesky:scheduled" in out


def test_status_lists_each_project_once_rather_than_once_per_article(herald, plan):
    """The N+1: a twelve-article campaign asked Herald for the same list twelve times."""
    sync(herald, plan)
    herald.calls.clear()

    campaign.status(herald, plan)

    assert herald.count("list_content") == 2  # two projects, three articles
    assert herald.count("list_projects") == 1


def test_status_says_so_when_a_project_was_never_created(herald, plan, capsys):
    campaign.status(herald, plan)
    assert capsys.readouterr().out.count("no project") == 3


def test_status_says_so_when_the_project_exists_but_the_article_does_not(
    herald, plan, capsys
):
    campaign.sync_projects(herald, plan, dry_run=False)
    campaign.status(herald, plan)
    assert capsys.readouterr().out.count("absent") == 3


# -- the CLI ----------------------------------------------------------------- #


def test_plan_is_offline_and_reports_the_word_counts(plan_path, capsys):
    assert campaign.main(["plan", str(plan_path)]) == 0
    out = capsys.readouterr().out
    assert "2 project(s), 3 article(s)" in out
    assert "Where ITC Leaks (Part 1)" in out


def test_a_bad_cadence_is_caught_before_the_plan_is_even_loaded(plan_path):
    """No login, no sync — the flag is wrong and nothing should have happened."""
    with pytest.raises(SystemExit, match="at least 1 day"):
        campaign.main(
            ["schedule", str(plan_path), "--start", "2099-01-01T09:00", "--every", "0"]
        )
