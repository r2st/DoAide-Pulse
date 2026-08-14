import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ReadTimePanel from "./ReadTimePanel";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: { readTime: vi.fn(), engagementTrend: vi.fn() },
}));

function band(name, max, overrides = {}) {
  return {
    band: name,
    max_read_minutes: max,
    publications: 0,
    avg_read_minutes: null,
    views: 0,
    reads: 0,
    clicks: 0,
    engagement: 0,
    reader_minutes: 0,
    avg_views: null,
    click_through_rate: null,
    read_rate: null,
    engagement_rate: null,
    ...overrides,
  };
}

/** A user with a healthy spread of lengths and platforms that count reads. */
function payload(overrides = {}) {
  return {
    published_pieces: 12,
    avg_read_minutes: 5.4,
    total_words: 14300,
    reader_minutes: 1840,
    publications_reporting_reads: 9,
    read_rate: 0.42,
    by_length: [
      band("short", 3, {
        publications: 8,
        avg_read_minutes: 2,
        views: 1000,
        reads: 200,
        engagement: 40,
        reader_minutes: 400,
        read_rate: 0.2,
        engagement_rate: 0.04,
      }),
      band("medium", 8, {
        publications: 2,
        avg_read_minutes: 6,
        views: 400,
        reads: 100,
        engagement: 32,
        reader_minutes: 600,
        read_rate: 0.25,
        engagement_rate: 0.08,
      }),
      band("long", null, {
        publications: 2,
        avg_read_minutes: 14,
        views: 400,
        reads: 60,
        engagement: 48,
        reader_minutes: 840,
        read_rate: 0.15,
        engagement_rate: 0.12,
      }),
    ],
    ...overrides,
  };
}

function trend(days, values) {
  return Array.from({ length: days + 1 }, (_, i) => ({
    date: `2026-07-${String(i + 1).padStart(2, "0")}`,
    views: 10,
    reads: 2,
    clicks: 1,
    engagement: 3,
    reader_minutes: values?.[i] ?? 0,
    click_through_rate: 0.1,
    read_rate: 0.2,
    engagement_rate: 0.3,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  api.readTime.mockResolvedValue(payload());
  api.engagementTrend.mockResolvedValue(trend(30, [0, 120, 90]));
});

describe("ReadTimePanel", () => {
  it("leads with the headline figures", async () => {
    render(<ReadTimePanel />);

    expect(await screen.findByText("1d 6h")).toBeInTheDocument(); // 1840 minutes
    expect(screen.getByText("5.4 min")).toBeInTheDocument();
    expect(screen.getByText("42.0%")).toBeInTheDocument();
    expect(screen.getByText("14,300 words published")).toBeInTheDocument();
    expect(
      screen.getByText("across 9 publications that count reads"),
    ).toBeInTheDocument();
  });

  it("answers whether length pays off, per view", async () => {
    render(<ReadTimePanel />);
    expect(
      await screen.findByText(
        "Long pieces earn 3.0× the engagement per view of short ones.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the length distribution and where the minutes went", async () => {
    render(<ReadTimePanel />);

    await screen.findByText("Length distribution");
    // Both panels label the bands the same way, hence getAllByText.
    expect(screen.getAllByText("Long · 9+ min").length).toBeGreaterThan(0);
    expect(screen.getByText(/14 min read on average/)).toBeInTheDocument();
    // 840 of 1840 reader-minutes sit in the long band against 2 of 12 pieces.
    expect(screen.getByText("17% / 46%")).toBeInTheDocument();
  });

  it("refetches the trend when the window changes", async () => {
    const user = userEvent.setup();
    render(<ReadTimePanel />);

    await waitFor(() => expect(api.engagementTrend).toHaveBeenCalledWith(30));
    await user.click(await screen.findByRole("button", { name: "7d" }));

    await waitFor(() => expect(api.engagementTrend).toHaveBeenCalledWith(7));
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps 'nobody counts reads' apart from 'nobody read it'", async () => {
    api.readTime.mockResolvedValue(
      payload({
        reader_minutes: 0,
        publications_reporting_reads: 0,
        read_rate: null,
        by_length: payload().by_length.map((b) => ({
          ...b,
          reads: 0,
          reader_minutes: 0,
          read_rate: null,
        })),
      }),
    );
    api.engagementTrend.mockResolvedValue(trend(30));
    render(<ReadTimePanel />);

    // A dash, not "0m" — the number is unknown, not zero.
    const caveat = await screen.findByText("No platform you publish to counts reads");
    expect(within(caveat.closest("div")).getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/Only Dev.to and Medium report reads/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "No platform you publish to counts reads, so there are no reader-minutes to plot.",
      ),
    ).toBeInTheDocument();
  });

  it("says so when there is not enough range to judge length", async () => {
    api.readTime.mockResolvedValue(
      payload({
        by_length: [
          band("short", 3, {
            publications: 4,
            avg_read_minutes: 2,
            views: 500,
            engagement: 20,
            reader_minutes: 100,
            engagement_rate: 0.04,
          }),
          band("medium", 8),
          band("long", null),
        ],
      }),
    );
    render(<ReadTimePanel />);
    expect(
      await screen.findByText(/Not enough range yet/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("nothing published at this length").length).toBe(4);
  });

  it("stays quiet until something has been published", async () => {
    api.readTime.mockResolvedValue({
      published_pieces: 0,
      avg_read_minutes: null,
      total_words: 0,
      reader_minutes: 0,
      publications_reporting_reads: 0,
      read_rate: null,
      by_length: [band("short", 3), band("medium", 8), band("long", null)],
    });
    render(<ReadTimePanel />);

    expect(
      await screen.findByText(/Nothing published yet/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Length distribution")).not.toBeInTheDocument();
  });

  it("offers a retry when the load fails", async () => {
    api.readTime.mockRejectedValue(new Error("gateway timeout"));
    render(<ReadTimePanel />);

    expect(await screen.findByText("gateway timeout")).toBeInTheDocument();

    api.readTime.mockResolvedValue(payload());
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("1d 6h")).toBeInTheDocument();
  });

  it("survives the trend failing on its own", async () => {
    api.engagementTrend.mockRejectedValue(new Error("trend unavailable"));
    render(<ReadTimePanel />);

    // The headline figures come from a different call and must still render.
    expect(await screen.findByText("1d 6h")).toBeInTheDocument();
    expect(screen.getByText("trend unavailable")).toBeInTheDocument();
  });
});

/**
 * The states between "loading" and "a healthy account".
 *
 * This panel's whole reason for existing is that it distinguishes an unknown
 * from a zero: no platform counting reads is not the same fact as nobody
 * reading, and the panel says so in four separate places. The existing tests
 * cover the fully-blind account. What was not covered is the account that is in
 * between — reads are counted, there just aren't any yet — where saying "we are
 * blind" would be as wrong as saying "nobody read it" is for the blind one.
 */
describe("the states on the way to a healthy account", () => {
  it("draws a placeholder rather than an empty panel while the figures load", () => {
    // A promise that never settles: the loading state is not a transient here,
    // it is what a slow analytics query looks like for several seconds.
    api.readTime.mockReturnValue(new Promise(() => {}));
    api.engagementTrend.mockReturnValue(new Promise(() => {}));
    const { container } = render(<ReadTimePanel />);

    expect(screen.getByText("Read time")).toBeInTheDocument();
    expect(container.querySelector("[aria-hidden='true']")).toBeInTheDocument();
    expect(screen.queryByText("Length distribution")).not.toBeInTheDocument();
  });

  it("keeps the figures up while only the trend is still loading", async () => {
    api.engagementTrend.mockReturnValue(new Promise(() => {}));
    render(<ReadTimePanel />);

    expect(await screen.findByText("1d 6h")).toBeInTheDocument();
    expect(screen.getByText("Length distribution")).toBeInTheDocument();
  });

  it("says reads are counted but absent, rather than that nothing counts them", async () => {
    // The distinction the whole panel is built around, from the other side:
    // nine publications *do* report reads, and every one of them reports zero.
    api.readTime.mockResolvedValue(
      payload({
        reader_minutes: 0,
        publications_reporting_reads: 9,
        read_rate: 0,
        by_length: payload().by_length.map((b) => ({
          ...b,
          reads: 0,
          reader_minutes: 0,
          read_rate: 0,
        })),
      }),
    );
    api.engagementTrend.mockResolvedValue(trend(30));
    render(<ReadTimePanel />);

    expect(await screen.findByText("No reads recorded yet.")).toBeInTheDocument();
    expect(
      screen.queryByText(/Only Dev.to and Medium report reads/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("No reads recorded in the last 30 days."),
    ).toBeInTheDocument();
  });

  it("names the window it found nothing in, and updates it when the window changes", async () => {
    const user = userEvent.setup();
    api.readTime.mockResolvedValue(payload({ publications_reporting_reads: 9 }));
    api.engagementTrend.mockResolvedValue(trend(30));
    render(<ReadTimePanel />);

    expect(
      await screen.findByText("No reads recorded in the last 30 days."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "7d" }));

    expect(
      await screen.findByText("No reads recorded in the last 7 days."),
    ).toBeInTheDocument();
  });

  it("dashes the average length rather than printing 'null min'", async () => {
    api.readTime.mockResolvedValue(payload({ avg_read_minutes: null }));
    render(<ReadTimePanel />);

    await screen.findByText("Length distribution");
    const stat = screen.getByText("Average length").closest("div");
    expect(within(stat).getByText("—")).toBeInTheDocument();
  });

  it("admits to having no metrics rather than reporting zero measured", async () => {
    api.readTime.mockResolvedValue(
      payload({ by_length: [band("short", 3), band("medium", 8), band("long", null)] }),
    );
    render(<ReadTimePanel />);

    expect(await screen.findByText("no metrics collected yet")).toBeInTheDocument();
  });

  it("counts the publications behind the bands when there are some", async () => {
    render(<ReadTimePanel />);

    // 8 + 2 + 2 across the three bands, which is not `published_pieces` — a
    // piece syndicated to three platforms is three publications.
    expect(await screen.findByText("12 publications measured")).toBeInTheDocument();
  });
});

describe("the window picker", () => {
  it("starts on 30 days with exactly one window selected", async () => {
    render(<ReadTimePanel />);

    await screen.findByText("Length distribution");
    const group = screen.getByRole("group", { name: "Window" });
    const buttons = within(group).getAllByRole("button");

    expect(buttons.map((b) => b.textContent)).toEqual(["7d", "30d", "90d"]);
    expect(
      buttons.filter((b) => b.getAttribute("aria-pressed") === "true"),
    ).toHaveLength(1);
    expect(within(group).getByRole("button", { name: "30d" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("un-presses the window it left", async () => {
    const user = userEvent.setup();
    render(<ReadTimePanel />);

    await user.click(await screen.findByRole("button", { name: "90d" }));

    await waitFor(() => expect(api.engagementTrend).toHaveBeenCalledWith(90));
    expect(screen.getByRole("button", { name: "30d" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("does not re-read the read-time payload just because the window moved", async () => {
    const user = userEvent.setup();
    render(<ReadTimePanel />);

    await screen.findByText("Length distribution");
    expect(api.readTime).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() => expect(api.engagementTrend).toHaveBeenCalledWith(7));
    // The expensive call is the other one; the window only re-reads the trend.
    expect(api.readTime).toHaveBeenCalledTimes(1);
  });

  it("offers a retry on a failed trend that does not disturb the rest", async () => {
    const user = userEvent.setup();
    api.engagementTrend.mockRejectedValueOnce(new Error("trend unavailable"));
    render(<ReadTimePanel />);

    await screen.findByText("trend unavailable");
    api.engagementTrend.mockResolvedValue(trend(30, [0, 120, 90]));

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.queryByText("trend unavailable")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("1d 6h")).toBeInTheDocument();
  });
});

describe("which band the payoff verdict points at", () => {
  /** The fill element of the bar row whose label starts with `name`. */
  function fill(name) {
    const row = screen
      .getAllByRole("img")
      .find((el) => el.getAttribute("aria-label").startsWith(name));
    return row.firstElementChild;
  }

  it("highlights the long band when long pieces win", async () => {
    render(<ReadTimePanel />);

    await screen.findByText(
      "Long pieces earn 3.0× the engagement per view of short ones.",
    );
    // The first three bars are the payoff panel's; the length distribution
    // below repeats the labels but is always brand-toned.
    expect(fill("Long")).toHaveClass("bg-brand-500/80");
    expect(fill("Short")).toHaveClass("bg-ink-400/40");
    expect(fill("Medium")).toHaveClass("bg-ink-400/40");
  });

  it("highlights the short band when short pieces win", async () => {
    const bands = payload().by_length;
    api.readTime.mockResolvedValue(
      payload({
        by_length: [
          { ...bands[0], engagement_rate: 0.12 },
          bands[1],
          { ...bands[2], engagement_rate: 0.04 },
        ],
      }),
    );
    render(<ReadTimePanel />);

    await screen.findByText(
      "Short pieces earn 3.0× the engagement per view of long ones.",
    );
    expect(fill("Short")).toHaveClass("bg-brand-500/80");
    expect(fill("Long")).toHaveClass("bg-ink-400/40");
  });

  it("highlights nothing while the verdict is that there is not enough range", async () => {
    api.readTime.mockResolvedValue(
      payload({
        by_length: [
          band("short", 3, {
            publications: 4,
            avg_read_minutes: 2,
            views: 500,
            engagement: 20,
            reader_minutes: 100,
            engagement_rate: 0.04,
          }),
          band("medium", 8),
          band("long", null),
        ],
      }),
    );
    render(<ReadTimePanel />);

    await screen.findByText(/Not enough range yet/);
    // Only the length-distribution panel below is brand-toned; the payoff
    // panel points at nothing, because it has nothing to point at.
    expect(fill("Short")).toHaveClass("bg-ink-400/40");
  });

  it("highlights nothing when the two ends come out even", async () => {
    const bands = payload().by_length;
    api.readTime.mockResolvedValue(
      payload({
        by_length: [
          { ...bands[0], engagement_rate: 0.05 },
          bands[1],
          { ...bands[2], engagement_rate: 0.05 },
        ],
      }),
    );
    render(<ReadTimePanel />);

    await screen.findByText(/Length is not deciding this/);
    expect(fill("Short")).toHaveClass("bg-ink-400/40");
    expect(fill("Long")).toHaveClass("bg-ink-400/40");
  });
});
