import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Analytics from "./Analytics";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    analytics: vi.fn(),
    engagementTrend: vi.fn(),
    velocity: vi.fn(),
    alerts: vi.fn(),
  },
}));

function totals(overrides = {}) {
  return {
    content_count: 12,
    published_count: 8,
    publication_count: 14,
    views: 4200,
    reads: 900,
    clicks: 210,
    engagement: 340,
    reads_reported: 6,
    clicks_reported: 6,
    click_through_rate: 0.05,
    read_rate: 0.214,
    engagement_rate: 0.081,
    ...overrides,
  };
}

/** A day of the trend series. Every day is present in the real payload. */
function day(date, values = {}) {
  return {
    date,
    views: 0,
    reads: 0,
    clicks: 0,
    engagement: 0,
    reader_minutes: 0,
    click_through_rate: null,
    read_rate: null,
    engagement_rate: null,
    ...values,
  };
}

function overview(overrides = {}) {
  return {
    totals: totals(),
    by_content_type: [
      {
        content_type: "announcement",
        label: "Announcement",
        publications: 5,
        views: 3000,
        reads: 700,
        clicks: 150,
        engagement: 260,
        avg_views: 600,
        click_through_rate: 0.05,
        read_rate: 0.23,
        engagement_rate: 0.086,
      },
    ],
    by_platform: [
      {
        platform: "devto",
        published: 6,
        failed: 1,
        views: 3800,
        reads: 850,
        clicks: 190,
        engagement: 300,
        click_through_rate: 0.05,
        read_rate: 0.22,
        engagement_rate: 0.078,
      },
      {
        platform: "bluesky",
        published: 8,
        failed: 0,
        views: 400,
        reads: 50,
        clicks: 20,
        engagement: 40,
        click_through_rate: 0.05,
        read_rate: 0.125,
        engagement_rate: 0.1,
      },
    ],
    by_project: [],
    top_content: [
      {
        content_id: 3,
        title: "Shipping the trigger engine",
        content_type: "announcement",
        project_id: 1,
        published_at: "2026-07-20T09:00:00Z",
        read_minutes: 6,
        views: 2400,
        engagement: 180,
        engagement_rate: 0.075,
      },
    ],
    timeline: [
      { date: "2026-07-28", publications: 1 },
      { date: "2026-07-29", publications: 0 },
      { date: "2026-07-30", publications: 2 },
    ],
    engagement_trend: [],
    read_time: {},
    ...overrides,
  };
}

function velocity(overrides = {}) {
  return {
    early_window_hours: 24,
    benchmark_window_hours: 48,
    publications: 14,
    benchmarks: [
      {
        platform: "devto",
        early_window_hours: 24,
        benchmark_window_hours: 48,
        median_early_views: 320,
        median_benchmark_views: 480,
        early_sample: 6,
        benchmark_sample: 6,
        reliable: true,
      },
      {
        platform: "bluesky",
        early_window_hours: 24,
        benchmark_window_hours: 48,
        median_early_views: 40,
        median_benchmark_views: 55,
        early_sample: 1,
        benchmark_sample: 1,
        reliable: false,
      },
    ],
    fastest: [
      {
        publication_id: 9,
        content_id: 3,
        platform: "devto",
        title: "Shipping the trigger engine",
        published_at: "2026-07-20T09:00:00Z",
        age_hours: 240,
        snapshots: 8,
        views: 2400,
        engagement: 180,
        views_first_24h: 900,
        views_first_48h: 1400,
        views_per_day: 240,
        stalled: false,
      },
    ],
    stalled: [
      {
        publication_id: 11,
        content_id: 5,
        platform: "bluesky",
        title: "A quieter post",
        published_at: "2026-07-01T09:00:00Z",
        age_hours: 720,
        snapshots: 12,
        views: 120,
        engagement: 4,
        views_first_24h: 100,
        views_first_48h: 110,
        views_per_day: 4,
        stalled: true,
      },
    ],
    ...overrides,
  };
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Analytics />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.analytics.mockResolvedValue(overview());
  api.engagementTrend.mockResolvedValue([
    day("2026-07-27", { views: 100, reads: 20, clicks: 5 }),
    day("2026-07-28", { views: 120, reads: 25, clicks: 6 }),
    day("2026-07-29", { views: 300, reads: 60, clicks: 15 }),
    day("2026-07-30", { views: 320, reads: 70, clicks: 18 }),
  ]);
  api.velocity.mockResolvedValue(velocity());
  api.alerts.mockResolvedValue({
    alerts: [
      {
        kind: "underperforming",
        severity: "warning",
        content_id: 5,
        publication_id: 11,
        platform: "bluesky",
        title: "A quieter post",
        message: "Took 27% of your usual first-day views on Bluesky.",
        ratio: 0.27,
        observed: 100,
        expected: 370,
      },
    ],
    warnings: 1,
    notices: 0,
  });
});

describe("the headline figures", () => {
  it("shows counts and rates side by side", async () => {
    draw();
    expect(await screen.findByText("4,200")).toBeInTheDocument();
    expect(screen.getByText("340")).toBeInTheDocument();
    expect(screen.getByText("5.0%")).toBeInTheDocument();
    expect(screen.getByText("21.4%")).toBeInTheDocument();
  });

  it("distinguishes a rate of zero from one nobody counts", async () => {
    // "—" says the platforms report nothing; "0.0%" would say nobody clicked.
    api.analytics.mockResolvedValue(
      overview({ totals: totals({ clicks_reported: 0, click_through_rate: null }) }),
    );
    draw();
    expect(await screen.findByText("no platform reports clicks")).toBeInTheDocument();
  });
});

describe("reach over time", () => {
  it("draws views, reads and clicks on one chart", async () => {
    draw();
    await screen.findByText("Views, reads and clicks");
    const chart = screen.getByRole("img", { name: /Views, reads and clicks per day/ });
    expect(chart).toHaveAccessibleName(/Views peaking at 320/);
    expect(chart).toHaveAccessibleName(/Reads peaking at 70/);
  });

  it("says which way the window is going", async () => {
    // 100+120 older against 300+320 newer.
    draw();
    expect(
      await screen.findByText("Views are 182% up on the previous 2 days."),
    ).toBeInTheDocument();
  });

  it("leaves clicks off when no platform counts them", async () => {
    api.analytics.mockResolvedValue(
      overview({ totals: totals({ clicks_reported: 0, click_through_rate: null }) }),
    );
    draw();
    const chart = await screen.findByRole("img", { name: /Views, reads and clicks per day/ });
    // An always-zero line along the baseline reads as "nobody clicked".
    expect(chart).not.toHaveAccessibleName(/Clicks/);
  });

  it("refetches when the window changes", async () => {
    draw();
    await screen.findByText("Views, reads and clicks");
    expect(api.engagementTrend).toHaveBeenCalledWith(30);

    await userEvent.click(screen.getByRole("button", { name: "90d" }));
    expect(api.engagementTrend).toHaveBeenCalledWith(90);
  });

  it("keeps the rest of the page when the trend alone fails", async () => {
    api.engagementTrend.mockRejectedValue(new Error("Trend unavailable"));
    draw();
    expect(await screen.findByText("Trend unavailable")).toBeInTheDocument();
    // The counters came from a different request and are still worth reading.
    expect(screen.getByText("4,200")).toBeInTheDocument();
  });
});

describe("what needs attention", () => {
  it("lists the underperformers and points at the piece", async () => {
    draw();
    expect(await screen.findByText("Underperforming")).toBeInTheDocument();
    expect(
      screen.getByText("Took 27% of your usual first-day views on Bluesky."),
    ).toBeInTheDocument();
    expect(screen.getByText("27% of usual")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /A quieter post/ })[0]).toHaveAttribute(
      "href",
      "/content/5",
    );
  });

  it("says nothing at all when there is nothing to say", async () => {
    api.alerts.mockResolvedValue({ alerts: [], warnings: 0, notices: 0 });
    draw();
    await screen.findByText("Reach over time");
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
  });
});

describe("where it lands", () => {
  it("names the platform that converts, not the one with the crowd", async () => {
    // Dev.to has nine times the views; Bluesky turns more of them into
    // something.
    draw();
    expect(
      await screen.findByText(
        "Bluesky turns a view into an interaction most often, at 10.0%.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps failures visible next to the view count", async () => {
    // "No views" and "every post was rejected" want different responses.
    draw();
    expect(await screen.findByText(/1 failed/)).toBeInTheDocument();
  });

  it("puts share of output against share of attention", async () => {
    draw();
    await screen.findByText("Effort against attention");
    expect(
      screen.getByRole("img", { name: "Devto, Posts: 43%" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Devto, Views: 90%" }),
    ).toBeInTheDocument();
  });
});

describe("velocity", () => {
  it("shows the median first day per platform", async () => {
    draw();
    expect(await screen.findByText("A normal first day")).toBeInTheDocument();
    expect(screen.getByText("320")).toBeInTheDocument();
    expect(screen.getByText("from 6 posts")).toBeInTheDocument();
  });

  it("says when a median is not yet a benchmark", async () => {
    // Reported because it is interesting, flagged because nothing should be
    // judged against a sample of one.
    draw();
    expect(
      await screen.findByText("only 1 post — not a benchmark yet"),
    ).toBeInTheDocument();
  });

  it("reads the early-window count from the configured window", async () => {
    draw();
    await screen.findByText("Fastest starts");
    expect(screen.getByText("900")).toBeInTheDocument();
  });

  it("follows a widened window rather than assuming 24 hours", async () => {
    api.velocity.mockResolvedValue(
      velocity({
        early_window_hours: 48,
        fastest: [
          {
            ...velocity().fastest[0],
            views_first_24h: 900,
            views_first_48h: 1400,
          },
        ],
      }),
    );
    draw();
    await screen.findByText("Fastest starts");
    expect(screen.getByText("1,400")).toBeInTheDocument();
  });

  it("lists what has stopped growing", async () => {
    draw();
    expect(await screen.findByText("Stopped growing")).toBeInTheDocument();
    expect(screen.getByText("4/day")).toBeInTheDocument();
  });
});

describe("a breakdown that came back with no rows", () => {
  // Distinct from "nothing has gone out yet", which the page catches earlier
  // and answers once. Here something *was* published and the grouping is
  // still empty, and the panel used to render as a bare header over a
  // hairline with a footnote about a comparison it could not make.
  it("says the platform panel has nothing to break down", async () => {
    api.analytics.mockResolvedValue(overview({ by_platform: [] }));
    draw();

    expect(await screen.findByText("No platform breakdown yet.")).toBeInTheDocument();
  });

  it("says the content-type panel has nothing to break down", async () => {
    api.analytics.mockResolvedValue(overview({ by_content_type: [] }));
    draw();

    expect(
      await screen.findByText("No content-type breakdown yet."),
    ).toBeInTheDocument();
  });

  it("still renders the rest of the page around an empty panel", async () => {
    api.analytics.mockResolvedValue(
      overview({ by_platform: [], by_content_type: [] }),
    );
    draw();

    // The counters are read straight off `totals` and owe nothing to either
    // breakdown, so an empty one must not take them down with it.
    expect(await screen.findByText("4,200")).toBeInTheDocument();
    expect(screen.getByText("Best performing")).toBeInTheDocument();
  });

  it("does not claim a best platform when there are no platforms", async () => {
    api.analytics.mockResolvedValue(overview({ by_platform: [] }));
    draw();

    expect(
      await screen.findByText(
        "Not enough views on any one platform yet to say which converts best.",
      ),
    ).toBeInTheDocument();
  });
});

describe("before anything has gone out", () => {
  it("says so once instead of showing eight empty panels", async () => {
    api.analytics.mockResolvedValue(
      overview({ totals: totals({ published_count: 0, views: 0 }) }),
    );
    draw();
    expect(await screen.findByText("Nothing has gone out yet")).toBeInTheDocument();
    expect(screen.queryByText("Reach over time")).not.toBeInTheDocument();
  });
});

describe("when the page cannot load", () => {
  it("offers a retry rather than a blank page", async () => {
    api.analytics.mockRejectedValue(new Error("Service unavailable"));
    draw();
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

/**
 * The three expensive sections, when they are slow or broken on their own.
 *
 * The page loads as four independent requests precisely so that the velocity
 * pass and the alert pass cannot hold the counters hostage. That arrangement is
 * only worth having if each section degrades by itself, and only the trend's
 * independent failure was covered — the two whose queries are the expensive
 * ones were not.
 */
describe("a section that is slow or broken on its own", () => {
  it("draws a placeholder for the whole page while the overview is still out", () => {
    api.analytics.mockReturnValue(new Promise(() => {}));
    api.engagementTrend.mockReturnValue(new Promise(() => {}));
    api.velocity.mockReturnValue(new Promise(() => {}));
    api.alerts.mockReturnValue(new Promise(() => {}));
    const { container } = draw();

    expect(screen.getByText("Analytics")).toBeInTheDocument();
    expect(container.querySelector("[aria-hidden='true']")).toBeInTheDocument();
    expect(screen.queryByText("Reach over time")).not.toBeInTheDocument();
  });

  it("keeps the counters up while the velocity pass is still running", async () => {
    api.velocity.mockReturnValue(new Promise(() => {}));
    draw();

    expect(await screen.findByText("4,200")).toBeInTheDocument();
    expect(screen.getByText("How fast it travels")).toBeInTheDocument();
    expect(screen.queryByText("A normal first day")).not.toBeInTheDocument();
  });

  it("offers a retry on the velocity pass without disturbing the page", async () => {
    api.velocity.mockRejectedValue(new Error("velocity timed out"));
    draw();

    expect(await screen.findByText("velocity timed out")).toBeInTheDocument();
    expect(screen.getByText("4,200")).toBeInTheDocument();
    expect(screen.getByText("Where it lands")).toBeInTheDocument();
  });

  it("offers a retry on the alert pass without disturbing the page", async () => {
    api.alerts.mockRejectedValue(new Error("alerts timed out"));
    draw();

    expect(await screen.findByText("alerts timed out")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("4,200")).toBeInTheDocument();
  });

  it("re-reads only the pass that failed when its retry is pressed", async () => {
    const user = userEvent.setup();
    api.velocity.mockRejectedValueOnce(new Error("velocity timed out"));
    draw();

    await screen.findByText("velocity timed out");
    api.velocity.mockResolvedValue(velocity());

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("A normal first day")).toBeInTheDocument();
    expect(api.analytics).toHaveBeenCalledTimes(1);
    expect(api.alerts).toHaveBeenCalledTimes(1);
  });
});

/**
 * Panels with nothing in them.
 *
 * The page is past its own "nothing published yet" gate by the time any of
 * these render, so an empty panel here means something narrower than "you are
 * new" — a query that returned no rows, a series with no snapshots old enough
 * to measure. Each says which, because the alternative is a panel whose body is
 * a single hairline, and a header with nothing under it reads as a bug.
 */
describe("panels with nothing to show", () => {
  it("says the median has nothing behind it yet", async () => {
    api.velocity.mockResolvedValue(velocity({ benchmarks: [] }));
    draw();

    await screen.findByText("A normal first day");
    expect(screen.getAllByText("Nothing measured yet.").length).toBeGreaterThan(0);
  });

  it("says nothing has a measured start yet", async () => {
    api.velocity.mockResolvedValue(velocity({ benchmarks: [], fastest: [] }));
    draw();

    await screen.findByText("Fastest starts");
    expect(screen.getAllByText("Nothing measured yet.")).toHaveLength(2);
  });

  it("reads an empty stalled list as good news rather than as no data", async () => {
    api.velocity.mockResolvedValue(velocity({ stalled: [] }));
    draw();

    expect(
      await screen.findByText(/Nothing has stalled/),
    ).toBeInTheDocument();
  });

  it("drops the velocity section entirely when nothing has been measured at all", async () => {
    // Not an empty panel — no section. Every sub-panel would be an empty state
    // repeating the same sentence, which is the case the page's own top-level
    // gate exists to avoid.
    api.velocity.mockResolvedValue(
      velocity({ publications: 0, benchmarks: [], fastest: [], stalled: [] }),
    );
    draw();

    await screen.findByText("Where it lands");
    expect(screen.queryByText("How fast it travels")).not.toBeInTheDocument();
  });

  it("says the top-content list has no metrics rather than showing an empty list", async () => {
    api.analytics.mockResolvedValue(overview({ top_content: [] }));
    draw();

    expect(await screen.findByText("No metrics collected yet.")).toBeInTheDocument();
    // The way out of the empty state is still there.
    expect(screen.getByRole("link", { name: "All content" })).toBeInTheDocument();
  });

  it("names the window the publishing rhythm found nothing in", async () => {
    api.analytics.mockResolvedValue(overview({ timeline: [] }));
    draw();

    expect(
      await screen.findByText("Nothing published in the last 30 days."),
    ).toBeInTheDocument();
  });

  it("names the window the reach chart found nothing in", async () => {
    api.engagementTrend.mockResolvedValue([]);
    draw();

    expect(
      await screen.findByText("Nothing recorded in the last 30 days."),
    ).toBeInTheDocument();
  });

  it("drops the effort-against-attention panel with only one platform", async () => {
    // A share is a comparison. One platform holds 100% of both bars, which
    // says nothing and looks like a finding.
    api.analytics.mockResolvedValue(
      overview({ by_platform: [overview().by_platform[0]] }),
    );
    draw();

    await screen.findByText("Where it lands");
    expect(screen.queryByText("Effort against attention")).not.toBeInTheDocument();
  });
});

/**
 * Numbers the API can send as null.
 *
 * Every one of these means "not measured", and every one of them renders as a
 * dash rather than a zero. A zero here is a claim — nobody clicked, nothing
 * gained a view all day — and it is a different claim from "nothing counted".
 */
describe("figures nobody has measured", () => {
  it("dashes a platform median rather than calling it zero", async () => {
    api.velocity.mockResolvedValue(
      velocity({
        benchmarks: [
          { ...velocity().benchmarks[0], median_early_views: null, early_sample: 0 },
        ],
      }),
    );
    draw();

    // Scoped to the benchmark panel: "Devto" also labels a bar in "Where it
    // lands" and a line in "Fastest starts".
    const panel = (await screen.findByText("A normal first day")).closest(".panel");
    const row = within(panel).getByText("Devto").closest("li");
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("dashes a stalled post's daily rate rather than calling it zero", async () => {
    api.velocity.mockResolvedValue(
      velocity({ stalled: [{ ...velocity().stalled[0], views_per_day: null }] }),
    );
    draw();

    // The same post is named in the alert list above.
    const panel = (await screen.findByText("Stopped growing")).closest(".panel");
    const row = within(panel).getByText("A quieter post").closest("li");
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(within(row).queryByText("0/day")).not.toBeInTheDocument();
  });

  it("dashes an unmeasured average view count in the content-type panel", async () => {
    api.analytics.mockResolvedValue(
      overview({
        by_content_type: [
          { ...overview().by_content_type[0], avg_views: null },
        ],
      }),
    );
    draw();

    expect(await screen.findByText(/5 published · 3,000 views · — avg/)).toBeInTheDocument();
  });

  it("says a benchmark drawn from one post is not a benchmark", async () => {
    // Singular, because "only 1 posts" is the kind of thing that survives
    // review and then reads as broken in production.
    api.velocity.mockResolvedValue(
      velocity({
        benchmarks: [
          { ...velocity().benchmarks[1], early_sample: 1, reliable: false },
        ],
      }),
    );
    draw();

    expect(
      await screen.findByText("only 1 post — not a benchmark yet"),
    ).toBeInTheDocument();
  });

  it("pluralises a sample of more than one", async () => {
    api.velocity.mockResolvedValue(
      velocity({
        benchmarks: [
          { ...velocity().benchmarks[1], early_sample: 3, reliable: false },
        ],
      }),
    );
    draw();

    expect(
      await screen.findByText("only 3 posts — not a benchmark yet"),
    ).toBeInTheDocument();
  });
});

describe("an alert row", () => {
  it("leaves out the ratio line when there is no ratio to show", async () => {
    api.alerts.mockResolvedValue({
      alerts: [
        {
          kind: "stalled",
          severity: "notice",
          content_id: 5,
          publication_id: 11,
          platform: "bluesky",
          title: "A quieter post",
          message: "Has not gained a view in four days.",
          ratio: null,
          observed: 100,
          expected: null,
        },
      ],
      warnings: 0,
      notices: 1,
    });
    draw();

    // The same post is named in the velocity panel below.
    const section = (await screen.findByText("Needs attention")).closest("section");
    const row = within(section).getByText("A quieter post").closest("a");
    expect(within(row).getByText("Open →")).toBeInTheDocument();
    expect(within(row).queryByText("%")).not.toBeInTheDocument();
  });

  it("keeps the section out of the way while the alert pass is still running", async () => {
    api.alerts.mockReturnValue(new Promise(() => {}));
    draw();

    await screen.findByText("Where it lands");
    // No header, no skeleton: an empty "Needs attention" above the numbers
    // reads as a finding, and this section is the one that most often has
    // nothing to report.
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
  });
});
