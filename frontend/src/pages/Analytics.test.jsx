import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Analytics from "./Analytics";
import { api } from "../lib/api";

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
    <MemoryRouter>
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
