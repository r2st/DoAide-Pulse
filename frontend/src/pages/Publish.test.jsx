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
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Publish from "./Publish";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

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
    <MemoryRouter future={ROUTER_FUTURE}>
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

describe("retrying", () => {
  const FAILED = [
    {
      id: 2,
      content_id: 8,
      platform: "mastodon",
      status: "failed",
      attempts: 3,
      error: "401 Unauthorized",
    },
  ];

  it("dispatches once however many times the button is clicked", async () => {
    // The one mutation on this page that had no in-flight guard, on the row
    // that dispatches to a broker. A second click re-armed the same
    // publication and queued a second task for it; only ``publish_one``'s
    // claim stopped that becoming a second post, and that guard is the wrong
    // one to be leaning on from here.
    api.publicationQueue.mockResolvedValue(FAILED);
    let release;
    api.retryPublication.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const user = userEvent.setup();
    draw();

    const button = await screen.findByRole("button", { name: "Retry" });
    await user.click(button);
    await user.click(button);
    await user.click(button);

    expect(api.retryPublication).toHaveBeenCalledTimes(1);
    expect(api.retryPublication).toHaveBeenCalledWith(8, 2);

    release({});
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Retrying"));
  });

  it("says it is retrying while the request is in flight", async () => {
    // Silence is what invited the second click: the label was the only thing
    // that could tell the user the first one landed.
    api.publicationQueue.mockResolvedValue(FAILED);
    let release;
    api.retryPublication.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Retry" }));

    const pending = screen.getByRole("button", { name: "Retrying…" });
    expect(pending).toBeDisabled();

    release({});
    await screen.findByRole("button", { name: "Retry" });
  });

  it("re-enables the button when the retry fails, so it can be tried again", async () => {
    // A guard that latches on is worse than no guard: the row it locks is a
    // failed publication, and the user has no other way to re-arm it.
    api.publicationQueue.mockResolvedValue(FAILED);
    api.retryPublication.mockRejectedValue(new Error("Bad Gateway"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Bad Gateway"));
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });
});

/**
 * The connection pill.
 *
 * Four states, three of which mean "this platform will not publish tonight" and
 * for three different reasons — no adapter, no key, a key the platform has
 * stopped accepting. The grid is where an operator looks to find out why a post
 * did not go out, and collapsing any two of these into one badge sends them to
 * the wrong fix.
 */
describe("what a platform tile says about the connection", () => {
  it("marks a platform with a working key as connected", async () => {
    draw();

    expect(await screen.findByText("connected")).toBeInTheDocument();
  });

  it("marks a platform with no key at all as not connected", async () => {
    api.platforms.mockResolvedValue([{ ...DEVTO, connection: null }]);
    draw();

    expect(await screen.findByText("not connected")).toBeInTheDocument();
  });

  it("marks a key the platform has stopped accepting as needing a reconnect", async () => {
    api.platforms.mockResolvedValue([
      { ...DEVTO, connection: { status: "invalid" } },
    ]);
    draw();

    expect(await screen.findByText("reconnect")).toBeInTheDocument();
  });

  it("marks an adapter Pulse has not written as not built, whatever the key says", async () => {
    // `implemented` wins over the connection: a stored credential for an
    // adapter that does not exist is not a working connection, and saying
    // "connected" would send someone hunting for a bug in the credential.
    api.platforms.mockResolvedValue([
      {
        platform: "medium",
        display_name: "Medium",
        implemented: false,
        connection: { status: "connected" },
      },
    ]);
    draw();

    expect(await screen.findByText("not built")).toBeInTheDocument();
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
  });

  it("counts failures on the tile, and only when there are some", async () => {
    api.analytics.mockResolvedValue({
      by_platform: [{ platform: "devto", published: 12, views: 3400, failed: 3 }],
      by_content_type: [],
      top_content: [],
    });
    draw();

    expect(await screen.findByText("3 failed")).toBeInTheDocument();
  });

  it("says nothing about failures when there are none", async () => {
    draw();

    await screen.findByText("12 published");
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument();
  });

  it("repeats the adapter's caveat on the tile", async () => {
    api.platforms.mockResolvedValue([
      { ...DEVTO, caveat: "Posts land as drafts; you publish them by hand." },
    ]);
    draw();

    expect(
      await screen.findByText("Posts land as drafts; you publish them by hand."),
    ).toBeInTheDocument();
  });
});

/**
 * The two sections below the grid, both of which appear only when analytics has
 * something to put in them.
 *
 * Each is a whole `<section>` behind a `length > 0` guard rather than an empty
 * state, which is right — a "What performs" header over nothing is a worse
 * answer than no header — but it means the guard is the only thing standing
 * between a working page and two headers with hairlines under them.
 */
describe("the performance sections", () => {
  const WITH_ANALYTICS = {
    by_platform: [{ platform: "devto", published: 12, views: 3400, failed: 0 }],
    by_content_type: [
      {
        content_type: "announcement",
        label: "Announcement",
        publications: 5,
        views: 3000,
        avg_views: 600,
      },
    ],
    top_content: [
      { content_id: 3, title: "Shipping the trigger engine", views: 2400, engagement: 180 },
    ],
  };

  it("breaks views down by kind of piece", async () => {
    api.analytics.mockResolvedValue(WITH_ANALYTICS);
    draw();

    const row = (await screen.findByText("Announcement")).closest("li");
    expect(within(row).getByText("5 pub")).toBeInTheDocument();
    expect(within(row).getByText("3,000 views")).toBeInTheDocument();
    expect(within(row).getByText("600 avg")).toBeInTheDocument();
  });

  it("dashes an average nobody has measured rather than calling it zero", async () => {
    api.analytics.mockResolvedValue({
      ...WITH_ANALYTICS,
      by_content_type: [{ ...WITH_ANALYTICS.by_content_type[0], avg_views: null }],
    });
    draw();

    const row = (await screen.findByText("Announcement")).closest("li");
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("links the best-performing pieces to their own editor", async () => {
    api.analytics.mockResolvedValue(WITH_ANALYTICS);
    draw();

    const link = await screen.findByRole("link", {
      name: "Shipping the trigger engine",
    });
    expect(link).toHaveAttribute("href", "/content/3");
    const row = link.closest("li");
    expect(within(row).getByText("2,400 views")).toBeInTheDocument();
    expect(within(row).getByText("180 eng")).toBeInTheDocument();
  });

  it("shows neither header when analytics has nothing to break down", async () => {
    draw();

    await screen.findByText("12 published");
    expect(screen.queryByText("What performs")).not.toBeInTheDocument();
    expect(screen.queryByText("Best performing")).not.toBeInTheDocument();
  });

  it("shows neither header while analytics is still out", async () => {
    // `analytics.data?.by_content_type?.length > 0` is `undefined > 0`, which
    // is false — the guard has to survive the loading state as well as the
    // empty one, and it does so by accident of the optional chaining.
    api.analytics.mockReturnValue(new Promise(() => {}));
    draw();

    await screen.findByText("Dev.to");
    expect(screen.queryByText("What performs")).not.toBeInTheDocument();
    expect(screen.queryByText("Best performing")).not.toBeInTheDocument();
  });
});

describe("what a queue row says", () => {
  function queued(overrides = {}) {
    return {
      id: 1,
      content_id: 7,
      platform: "devto",
      status: "pending",
      scheduled_for: null,
      attempts: 0,
      error: null,
      ...overrides,
    };
  }

  it("says a pending row is waiting on a worker rather than showing no time", async () => {
    api.publicationQueue.mockResolvedValue([queued()]);
    draw();

    expect(
      await screen.findByText(/as soon as a worker picks it up/),
    ).toBeInTheDocument();
  });

  it("counts a single attempt in the singular", async () => {
    api.publicationQueue.mockResolvedValue([
      queued({ status: "failed", attempts: 1, error: "401 Unauthorized" }),
    ]);
    draw();

    expect(await screen.findByText(/1 attempt(?!s)/)).toBeInTheDocument();
  });

  it("counts several attempts in the plural", async () => {
    api.publicationQueue.mockResolvedValue([
      queued({ status: "failed", attempts: 3, error: "401 Unauthorized" }),
    ]);
    draw();

    expect(await screen.findByText(/3 attempts/)).toBeInTheDocument();
  });

  it("says nothing about attempts on a row that has not been tried", async () => {
    api.publicationQueue.mockResolvedValue([queued()]);
    draw();

    await screen.findByText(/as soon as a worker/);
    expect(screen.queryByText(/attempt/)).not.toBeInTheDocument();
  });

  it("shows what the platform actually said when it refused", async () => {
    api.publicationQueue.mockResolvedValue([
      queued({ status: "failed", attempts: 1, error: "422 title too long" }),
    ]);
    draw();

    expect(await screen.findByText("422 title too long")).toBeInTheDocument();
  });

  it("links the row to the piece it is trying to publish", async () => {
    api.publicationQueue.mockResolvedValue([queued()]);
    draw();

    expect(await screen.findByRole("link", { name: "Devto" })).toHaveAttribute(
      "href",
      "/content/7",
    );
  });

  it("draws a placeholder rather than 'nothing queued' while the queue loads", async () => {
    // The wrong answer here is the confident one: an empty state that says
    // "Nothing queued" over a queue that has not arrived tells the user their
    // posts are gone.
    api.publicationQueue.mockReturnValue(new Promise(() => {}));
    const { container } = draw();

    // Waits for the two calls that *do* answer, so their state lands inside
    // `act`; the queue is still out at this point and stays out.
    await screen.findByText("Dev.to");
    expect(container.querySelector("[aria-hidden='true']")).toBeInTheDocument();
    expect(screen.queryByText("Nothing queued")).not.toBeInTheDocument();
  });
});
