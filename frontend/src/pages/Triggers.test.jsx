import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Triggers from "./Triggers";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    listProjects: vi.fn(),
    triggerKinds: vi.fn(),
    listTriggers: vi.fn(),
    createTrigger: vi.fn(),
    updateTrigger: vi.fn(),
    deleteTrigger: vi.fn(),
    checkTrigger: vi.fn(),
    rotateTriggerSecret: vi.fn(),
    triggerEvents: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({
  useToast: () => toast,
}));

const KINDS = [
  {
    kind: "github",
    label: "GitHub",
    description: "A watched repository shipped commits or cut a release.",
    required_config: [],
    optional_config: [],
  },
  {
    kind: "webhook",
    label: "Webhook",
    description: "Anything that can send an HTTP request POSTs to a URL.",
    required_config: [],
    optional_config: [],
  },
  {
    kind: "rss",
    label: "RSS",
    description: "An RSS or Atom feed gained an entry.",
    required_config: ["feed_url"],
    optional_config: [],
  },
  {
    kind: "schedule",
    label: "Schedule",
    description: "Time passed. No external event.",
    required_config: [],
    optional_config: [],
  },
];

function trigger(overrides = {}) {
  return {
    id: 1,
    project_id: 7,
    kind: "rss",
    name: "Changelog feed",
    is_active: true,
    config: { feed_url: "https://example.com/changelog.rss" },
    inbound_url: null,
    has_secret: false,
    last_checked_at: null,
    last_fired_at: null,
    fire_count: 2,
    consecutive_failures: 0,
    last_error: null,
    created_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Triggers />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listProjects.mockResolvedValue([{ id: 7, name: "Herald" }]);
  api.triggerKinds.mockResolvedValue(KINDS);
  api.listTriggers.mockResolvedValue([trigger()]);
  api.triggerEvents.mockResolvedValue([]);
});

describe("the list", () => {
  it("shows what each trigger watches, not just its name", async () => {
    draw();
    expect(await screen.findByText("Changelog feed")).toBeInTheDocument();
    expect(screen.getByText("https://example.com/changelog.rss")).toBeInTheDocument();
  });

  it("says a trigger is failing even while it is still active", async () => {
    // The state an on/off list hides: enabled, but not working.
    api.listTriggers.mockResolvedValue([
      trigger({ consecutive_failures: 4, last_error: "404 Not Found" }),
    ]);
    draw();
    expect(await screen.findByText("Failing (4×)")).toBeInTheDocument();
    expect(screen.getByText("404 Not Found")).toBeInTheDocument();
  });

  it("shows a hostile error message as text rather than markup", async () => {
    // `last_error` is the remote end's own words: for a webhook it is an
    // excerpt of the response body, for a feed whatever the server said. None
    // of it is Herald's, so the endpoint picks this string, not the user. It
    // reaches the DOM as a JSX child, which React escapes — pinned here so a
    // later "render the error as rich text" never quietly makes it a payload.
    const hostile = '<img src=x onerror="alert(1)">';
    api.listTriggers.mockResolvedValue([
      trigger({ consecutive_failures: 1, last_error: hostile }),
    ]);
    draw();

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });

  it("does not claim a trigger that has never fired is healthy", async () => {
    api.listTriggers.mockResolvedValue([trigger({ fire_count: 0 })]);
    draw();
    expect(await screen.findByText("Never fired")).toBeInTheDocument();
  });

  it("offers Check now only for the kinds Herald polls", async () => {
    api.listTriggers.mockResolvedValue([
      trigger({ id: 1, kind: "rss" }),
      trigger({
        id: 2,
        kind: "webhook",
        name: "Zapier",
        config: {},
        inbound_url: "https://herald.test/api/v1/triggers/inbound/tok",
      }),
    ]);
    draw();
    await screen.findByText("Changelog feed");
    // One polled trigger on the page, so exactly one button.
    expect(screen.getAllByRole("button", { name: "Check now" })).toHaveLength(1);
  });

  it("shows an inbound webhook's URL so it can be pasted somewhere", async () => {
    api.listTriggers.mockResolvedValue([
      trigger({
        kind: "webhook",
        config: {},
        inbound_url: "https://herald.test/api/v1/triggers/inbound/tok",
      }),
    ]);
    draw();
    expect(
      await screen.findByText("https://herald.test/api/v1/triggers/inbound/tok"),
    ).toBeInTheDocument();
  });

  it("warns that an unsigned inbound URL is the whole credential", async () => {
    api.listTriggers.mockResolvedValue([
      trigger({ kind: "webhook", config: {}, inbound_url: "https://herald.test/x" }),
    ]);
    draw();
    expect(await screen.findByText(/Anyone holding this URL can fire it/)).toBeInTheDocument();
  });
});

describe("empty and error states", () => {
  it("sends a user with no projects to make one, rather than to a dead end", async () => {
    api.listProjects.mockResolvedValue([]);
    api.listTriggers.mockResolvedValue([]);
    draw();
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a project" })).toHaveAttribute(
      "href",
      "/projects",
    );
  });

  it("cannot open the builder with no project to attach a trigger to", async () => {
    api.listProjects.mockResolvedValue([]);
    api.listTriggers.mockResolvedValue([]);
    draw();
    await screen.findByText("No projects yet");
    expect(screen.getByRole("button", { name: "Add trigger" })).toBeDisabled();
  });

  it("offers the builder from the empty state when there is a project", async () => {
    api.listTriggers.mockResolvedValue([]);
    draw();
    expect(await screen.findByText("Nothing is watching yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add your first trigger" })).toBeEnabled();
  });

  it("surfaces a failed load with a retry", async () => {
    api.listTriggers.mockRejectedValueOnce(new Error("Service unavailable"));
    draw();
    expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
  });
});

describe("checking a trigger on demand", () => {
  it("reports a first look as connected rather than as nothing happening", async () => {
    api.checkTrigger.mockResolvedValue({ status: "baselined", entries: 12 });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(toast.success.mock.calls[0][0]).toMatch(/12 entries/);
  });

  it("shows a bad feed URL as an error, not a success", async () => {
    api.checkTrigger.mockResolvedValue({ status: "error", error: "That host is private." });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("That host is private."));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("reloads the list, so last-checked and the failure count are current", async () => {
    api.checkTrigger.mockResolvedValue({ status: "no_news" });
    draw();
    await screen.findByText("Changelog feed");
    const before = api.listTriggers.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() =>
      expect(api.listTriggers.mock.calls.length).toBeGreaterThan(before),
    );
  });
});

describe("pausing", () => {
  it("pauses an active trigger and resumes a paused one", async () => {
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(api.updateTrigger).toHaveBeenCalledWith(1, { is_active: false }),
    );

    api.listTriggers.mockResolvedValue([trigger({ is_active: false })]);
    draw();
    const resume = await screen.findAllByRole("button", { name: "Resume" });
    await userEvent.click(resume[0]);
    await waitFor(() =>
      expect(api.updateTrigger).toHaveBeenCalledWith(1, { is_active: true }),
    );
  });
});

describe("the builder", () => {
  it("shows only the fields the chosen kind accepts", async () => {
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));

    const dialog = screen.getByRole("dialog");
    // Defaults to RSS.
    expect(within(dialog).getByLabelText(/Feed URL/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Topic/)).not.toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("radio", { name: /Schedule/ }));
    expect(within(dialog).getByLabelText(/Topic/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Feed URL/)).not.toBeInTheDocument();
  });

  it("keeps what was typed when the kind is switched and switched back", async () => {
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");

    await userEvent.type(
      within(dialog).getByLabelText(/Feed URL/),
      "https://example.com/f.xml",
    );
    await userEvent.click(within(dialog).getByRole("radio", { name: /Schedule/ }));
    await userEvent.click(within(dialog).getByRole("radio", { name: /RSS/ }));

    expect(within(dialog).getByLabelText(/Feed URL/)).toHaveValue("https://example.com/f.xml");
  });

  it("does not send a field belonging to a kind that was abandoned", async () => {
    // The API refuses unknown keys outright, so a leftover must not go with it.
    api.createTrigger.mockResolvedValue(trigger({ kind: "schedule" }));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");

    await userEvent.type(within(dialog).getByLabelText(/Feed URL/), "https://example.com/f.xml");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Schedule/ }));
    await userEvent.type(within(dialog).getByLabelText(/Topic/), "Weekly roundup");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    await waitFor(() => expect(api.createTrigger).toHaveBeenCalled());
    const payload = api.createTrigger.mock.calls[0][0];
    expect(payload.kind).toBe("schedule");
    expect(payload.config).not.toHaveProperty("feed_url");
    expect(payload.config.topic).toBe("Weekly roundup");
  });

  it("reports a rejected config instead of closing as though it saved", async () => {
    api.createTrigger.mockRejectedValue(new Error("A rss trigger needs feed_url."));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Feed URL/), "not-a-url");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("A rss trigger needs feed_url."),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("cannot change a trigger's kind after it exists", async () => {
    // The stored watermark belongs to the kind that wrote it.
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("radio")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Feed URL/)).toHaveValue(
      "https://example.com/changelog.rss",
    );
  });
});

describe("the signing secret", () => {
  it("is shown once, with the URL, after creating a webhook trigger", async () => {
    api.createTrigger.mockResolvedValue({
      ...trigger({ kind: "webhook", config: {} }),
      inbound_url: "https://herald.test/api/v1/triggers/inbound/tok",
      secret: "s3cr3t-value",
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Webhook/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    expect(await screen.findByText("s3cr3t-value")).toBeInTheDocument();
    expect(screen.getByText(/never again/)).toBeInTheDocument();
  });

  it("is not shown for a kind that has no inbound URL", async () => {
    api.createTrigger.mockResolvedValue(trigger());
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Feed URL/), "https://example.com/f.xml");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Trigger saved"));
    expect(screen.queryByText(/never again/)).not.toBeInTheDocument();
  });
});

describe("activity", () => {
  it("lists firings and links the draft one produced", async () => {
    api.triggerEvents.mockResolvedValue([
      {
        id: 5,
        trigger_id: 1,
        headline: "v1.2.0 released",
        status: "generated",
        detail: "",
        content_id: 42,
        dedupe_key: "abc",
        payload: {},
        created_at: "2026-07-30T10:00:00Z",
      },
    ]);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Activity" }));

    expect(await screen.findByText("v1.2.0 released")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open draft" })).toHaveAttribute(
      "href",
      "/content/42",
    );
  });

  it("explains why a firing wrote nothing", async () => {
    api.triggerEvents.mockResolvedValue([
      {
        id: 6,
        trigger_id: 1,
        headline: "New entry",
        status: "skipped",
        detail: "Daily limit reached",
        content_id: null,
        dedupe_key: null,
        payload: {},
        created_at: "2026-07-30T10:00:00Z",
      },
    ]);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Activity" }));

    expect(await screen.findByText("Daily limit reached")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open draft" })).not.toBeInTheDocument();
  });

  it("says so when a trigger has never fired", async () => {
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Activity" }));
    expect(await screen.findByText(/Nothing yet/)).toBeInTheDocument();
  });
});

describe("filtering by project", () => {
  it("narrows the request rather than filtering in the browser", async () => {
    draw();
    await screen.findByText("Changelog feed");
    await userEvent.selectOptions(screen.getByLabelText("Filter by project"), "7");
    await waitFor(() => expect(api.listTriggers).toHaveBeenCalledWith({ project_id: "7" }));
  });
});
