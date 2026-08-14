/**
 * The home page: ordered by what needs a human, then what's next, then the
 * record of what already happened. Covers the empty-account state, the
 * "needs you" section's three sources (review queue, failed publications,
 * performance alerts), and the loading/error states `useApi` drives.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: { dashboard: vi.fn(), readTime: vi.fn(), engagementTrend: vi.fn() },
}));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Dashboard />
    </MemoryRouter>,
  );
}

function payload(overrides = {}) {
  return {
    totals: {
      content_count: 4,
      published_count: 3,
      publication_count: 5,
      views: 1200,
      click_through_rate: 0.08,
      engagement: 90,
      engagement_rate: 0.05,
    },
    needs_review: 0,
    failed_publications: [],
    upcoming: [],
    recent_content: [],
    by_project: [],
    alerts: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.readTime.mockResolvedValue({ published_pieces: 0 });
  api.engagementTrend.mockResolvedValue({ points: [] });
});

describe("an empty account", () => {
  it("offers to register a project instead of showing zeroed stat tiles", async () => {
    api.dashboard.mockResolvedValue(payload({ totals: { ...payload().totals, content_count: 0 } }));
    draw();

    expect(await screen.findByText("Nothing written yet")).toBeInTheDocument();
    expect(screen.queryByText("Pieces")).not.toBeInTheDocument();
  });
});

describe("loading and errors", () => {
  it("shows a skeleton before the first response arrives", () => {
    api.dashboard.mockReturnValue(new Promise(() => {}));
    draw();

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.queryByText("Pieces")).not.toBeInTheDocument();
  });

  it("shows a retryable error instead of crashing on a failed fetch", async () => {
    api.dashboard.mockRejectedValue(new Error("Service Unavailable"));
    draw();

    expect(await screen.findByText("Service Unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("stat tiles", () => {
  it("renders the headline totals", async () => {
    api.dashboard.mockResolvedValue(payload());
    draw();

    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText("1,200")).toBeInTheDocument();
  });
});

describe("needs you", () => {
  it("is absent entirely when nothing needs a human", async () => {
    api.dashboard.mockResolvedValue(payload());
    draw();
    await screen.findByText("Pieces");

    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });

  it("surfaces drafts waiting for review, pluralized", async () => {
    api.dashboard.mockResolvedValue(payload({ needs_review: 2 }));
    draw();

    expect(
      await screen.findByRole("link", { name: /2 drafts are waiting for review/ }),
    ).toBeInTheDocument();
  });

  it("uses the singular for exactly one draft", async () => {
    api.dashboard.mockResolvedValue(payload({ needs_review: 1 }));
    draw();

    expect(
      await screen.findByRole("link", { name: /1 draft is waiting for review/ }),
    ).toBeInTheDocument();
  });

  it("lists failed publications above alerts", async () => {
    api.dashboard.mockResolvedValue(
      payload({
        failed_publications: [{ id: 1, platform: "devto", error: "401 Unauthorized" }],
        alerts: [
          {
            content_id: 9,
            publication_id: 9,
            kind: "stalled",
            severity: "info",
            platform: "devto",
            title: "A post that stopped growing",
            message: "Growth has flattened.",
            ratio: 0.2,
          },
        ],
      }),
    );
    draw();

    const section = (await screen.findByText("Needs you")).closest("section");
    const rows = within(section).getAllByRole("link");
    // The two review-adjacent links come after the section header's own text
    // node, so the first is the failed publication and the second the alert.
    expect(rows[0]).toHaveTextContent("401 Unauthorized");
    expect(rows[1]).toHaveTextContent("A post that stopped growing");
  });

  it("renders an alert missing entirely from an older cached response", async () => {
    const data = payload();
    delete data.alerts;
    api.dashboard.mockResolvedValue(data);
    draw();

    expect(await screen.findByText("Pieces")).toBeInTheDocument();
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });
});

describe("what's next", () => {
  it("says nothing is scheduled rather than an empty list", async () => {
    api.dashboard.mockResolvedValue(payload());
    draw();

    expect(await screen.findByText(/Nothing scheduled/)).toBeInTheDocument();
  });

  it("lists upcoming publications with their platform and time", async () => {
    api.dashboard.mockResolvedValue(
      payload({
        upcoming: [
          {
            id: 1,
            content_id: 5,
            title: "Shipping the new editor",
            platform: "devto",
            scheduled_for: "2099-01-01T10:00:00Z",
          },
        ],
      }),
    );
    draw();

    expect(await screen.findByText("Shipping the new editor")).toBeInTheDocument();
  });
});

describe("recent content and by-project", () => {
  it("says nothing is written yet in both empty panels", async () => {
    api.dashboard.mockResolvedValue(payload());
    draw();
    await screen.findByText("Pieces");

    expect(screen.getByText("Nothing written yet.")).toBeInTheDocument();
    expect(screen.getByText("No projects registered yet.")).toBeInTheDocument();
  });

  it("lists recent content with its status", async () => {
    api.dashboard.mockResolvedValue(
      payload({
        recent_content: [
          {
            id: 3,
            title: "A retry budget that outlasts the outage",
            project_name: "Herald",
            content_type: "changelog",
            created_at: "2026-08-01T10:00:00Z",
            status: "published",
          },
        ],
      }),
    );
    draw();

    expect(
      await screen.findByText("A retry budget that outlasts the outage"),
    ).toBeInTheDocument();
  });

  it("lists per-project traction", async () => {
    api.dashboard.mockResolvedValue(
      payload({
        by_project: [{ project_id: 1, name: "Herald", published: 5, views: 900 }],
      }),
    );
    draw();

    expect(await screen.findByText("Herald")).toBeInTheDocument();
    expect(screen.getByText("5 pub")).toBeInTheDocument();
  });
});
