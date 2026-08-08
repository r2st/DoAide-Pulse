/**
 * The Publish page, and the two fetches it used to make silently.
 *
 * The page makes three calls — the queue, the platform list, and analytics —
 * and reported errors from only the first. Every other page here banners each
 * fetch it makes (Analytics banners all four of its own), so the two silent
 * ones were an oversight rather than a policy, and the failure mode was the bad
 * kind: a 502 from `/settings/platforms` rendered as an empty grid, and a
 * failed analytics call fell through to `?? 0` and printed "0 published,
 * 0 views" against platforms that had plenty. An empty page says "I don't
 * know"; a zero says "I do", and it was wrong.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Publish from "./Publish";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    publicationQueue: vi.fn(),
    platforms: vi.fn(),
    analytics: vi.fn(),
    retryPublication: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

function draw() {
  return render(
    <MemoryRouter>
      <Publish />
    </MemoryRouter>,
  );
}

const DEVTO = {
  platform: "devto",
  display_name: "Dev.to",
  implemented: true,
  connection: { status: "connected" },
};

beforeEach(() => {
  vi.clearAllMocks();
  api.publicationQueue.mockResolvedValue([]);
  api.platforms.mockResolvedValue([DEVTO]);
  api.analytics.mockResolvedValue({
    by_platform: [{ platform: "devto", published: 12, views: 3400, failed: 0 }],
    by_content_type: [],
    top_content: [],
  });
});

describe("reporting what failed", () => {
  it("surfaces a failed platform list instead of an empty grid", async () => {
    api.platforms.mockRejectedValue(new Error("Bad Gateway"));
    draw();

    expect(await screen.findByText("Bad Gateway")).toBeInTheDocument();
  });

  it("surfaces a failed analytics call", async () => {
    api.analytics.mockRejectedValue(new Error("Service Unavailable"));
    draw();

    expect(await screen.findByText("Service Unavailable")).toBeInTheDocument();
  });

  it("still surfaces a failed queue", async () => {
    api.publicationQueue.mockRejectedValue(new Error("Gone"));
    draw();

    expect(await screen.findByText("Gone")).toBeInTheDocument();
  });

  it("shows no alert at all when every call succeeds", async () => {
    draw();
    await screen.findByText("Dev.to");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("reach figures", () => {
  it("shows the real numbers once analytics has answered", async () => {
    draw();

    const tile = (await screen.findByText("Dev.to")).closest("div.panel");
    expect(within(tile).getByText(/12 published/)).toBeInTheDocument();
    expect(within(tile).getByText(/3,400 views/)).toBeInTheDocument();
  });

  it("shows a dash, not a zero, when analytics never answered", async () => {
    api.analytics.mockRejectedValue(new Error("Service Unavailable"));
    draw();

    const tile = (await screen.findByText("Dev.to")).closest("div.panel");
    expect(within(tile).getByText(/— published/)).toBeInTheDocument();
    expect(within(tile).getByText(/— views/)).toBeInTheDocument();
    expect(within(tile).queryByText(/0 published/)).not.toBeInTheDocument();
  });

  it("still shows a genuine zero for a platform analytics has no row for", async () => {
    // Answered, with nothing for Dev.to — that really is nought, and must not
    // be reported as unknown.
    api.analytics.mockResolvedValue({
      by_platform: [],
      by_content_type: [],
      top_content: [],
    });
    draw();

    const tile = (await screen.findByText("Dev.to")).closest("div.panel");
    expect(within(tile).getByText(/0 published/)).toBeInTheDocument();
    expect(within(tile).queryByText(/— published/)).not.toBeInTheDocument();
  });
});

describe("the queue", () => {
  it("leads with failures, above the pending queue", async () => {
    api.publicationQueue.mockResolvedValue([
      { id: 1, content_id: 7, platform: "devto", status: "pending", attempts: 0 },
      {
        id: 2,
        content_id: 8,
        platform: "mastodon",
        status: "failed",
        attempts: 3,
        error: "401 Unauthorized",
      },
    ]);
    draw();

    await screen.findByText("Failed");
    const headings = screen.getAllByText(/^(Failed|Queue)$/);
    expect(headings.map((h) => h.textContent)).toEqual(["Failed", "Queue"]);
  });

  it("offers a retry only on the rows that failed", async () => {
    api.publicationQueue.mockResolvedValue([
      { id: 1, content_id: 7, platform: "devto", status: "pending", attempts: 0 },
      {
        id: 2,
        content_id: 8,
        platform: "mastodon",
        status: "failed",
        attempts: 3,
        error: "401 Unauthorized",
      },
    ]);
    draw();

    await screen.findByText("401 Unauthorized");
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });

  it("offers a retry on a row waiting out its backoff", async () => {
    // The backend parks a retryable failure as `scheduled`, up to an hour out.
    // Somebody looking at that row has usually just fixed what broke, and
    // "in an hour" is the wrong answer to give them.
    api.publicationQueue.mockResolvedValue([
      {
        id: 3,
        content_id: 9,
        platform: "devto",
        status: "scheduled",
        attempts: 1,
        scheduled_for: "2099-01-01T10:00:00Z",
        error: "devto is down — retrying in 300s",
      },
    ]);
    draw();

    await screen.findByText(/retrying in 300s/);
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });

  it("offers no retry on a post that is merely scheduled", async () => {
    // Nothing has gone wrong with this one. A Retry button on it would read as
    // "publish now", which is a different action and lives on the calendar.
    api.publicationQueue.mockResolvedValue([
      {
        id: 4,
        content_id: 10,
        platform: "devto",
        status: "scheduled",
        attempts: 0,
        scheduled_for: "2099-01-01T10:00:00Z",
      },
    ]);
    draw();

    await screen.findByText("Dev.to");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("says nothing is queued rather than showing an empty list", async () => {
    draw();
    expect(await screen.findByText("Nothing queued")).toBeInTheDocument();
  });
});
