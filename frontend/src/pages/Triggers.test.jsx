import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

  it("says '1 time' rather than '1 times'", async () => {
    api.listTriggers.mockResolvedValue([trigger({ fire_count: 1 })]);
    draw();
    expect(await screen.findByText(/fired 1 time$/)).toBeInTheDocument();
  });

  it("pluralises every other count", async () => {
    api.listTriggers.mockResolvedValue([trigger({ fire_count: 2 })]);
    draw();
    expect(await screen.findByText(/fired 2 times$/)).toBeInTheDocument();
  });

  it("shows when a trigger last fired, and says nothing when it has not", async () => {
    api.listTriggers.mockResolvedValue([
      trigger({ fire_count: 3, last_fired_at: "2026-08-14T09:00:00Z" }),
    ]);
    const { unmount } = draw();
    expect(await screen.findByText(/^last /)).toBeInTheDocument();
    unmount();

    api.listTriggers.mockResolvedValue([trigger({ fire_count: 0, last_fired_at: null })]);
    draw();
    await screen.findByText("Changelog feed");
    expect(screen.queryByText(/^last /)).not.toBeInTheDocument();
  });

  it("names the cadence and content type a schedule was configured with", async () => {
    // Both are optional on every kind, and a schedule that fires every 6h into
    // changelogs is a different trigger from one that fires daily into
    // announcements — the list is unreadable if it shows neither.
    api.listTriggers.mockResolvedValue([
      trigger({
        kind: "schedule",
        config: { every_hours: 6, content_type: "changelog" },
      }),
    ]);
    draw();
    expect(await screen.findByText("every 6h")).toBeInTheDocument();
    expect(screen.getByText("Changelog")).toBeInTheDocument();
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

  it("drops the warning once the URL is signed, rather than hedging", async () => {
    // The other half of the pair above, and the reason it matters: the warning
    // is only useful if its absence means something. A line that shows either
    // way tells the reader nothing about which state they are in.
    api.listTriggers.mockResolvedValue([
      trigger({
        kind: "webhook",
        config: { require_signature: true },
        inbound_url: "https://herald.test/x",
        has_secret: true,
      }),
    ]);
    draw();
    expect(
      await screen.findByText(/Requests must carry a valid signature/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Anyone holding this URL/)).not.toBeInTheDocument();
  });

  it("shows when a polled trigger was last looked at, not only when it fired", async () => {
    // Fired and checked are different questions, and a feed that is polled
    // hourly and has fired twice all year looks broken unless the page can say
    // it was checked ten minutes ago.
    api.listTriggers.mockResolvedValue([
      trigger({ last_checked_at: "2026-08-14T09:00:00Z", fire_count: 2 }),
    ]);
    draw();
    expect(await screen.findByTitle("Last poll")).toHaveTextContent(/^checked /);
  });

  it("does not offer a last-poll time for a kind nothing polls", async () => {
    // A webhook is never checked, so a stale `last_checked_at` on one is a
    // field the card must ignore rather than render as fact.
    api.listTriggers.mockResolvedValue([
      trigger({
        kind: "webhook",
        config: {},
        inbound_url: "https://herald.test/x",
        last_checked_at: "2026-08-14T09:00:00Z",
      }),
    ]);
    draw();
    await screen.findByText("Changelog feed");
    expect(screen.queryByTitle("Last poll")).not.toBeInTheDocument();
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

  it("the empty state's button opens the same builder the header does", async () => {
    // Enabled was asserted above; that it is wired to anything was not. It is a
    // separate element from the header's, and the only one on screen for an
    // account that has never made a trigger.
    api.listTriggers.mockResolvedValue([]);
    draw();
    await screen.findByText("Nothing is watching yet");

    await userEvent.click(screen.getByRole("button", { name: "Add your first trigger" }));

    expect(screen.getByRole("dialog")).toBeInTheDocument();
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

  it("sends the settings every kind shares, in the types the API stores them as", async () => {
    // Three controls of three different types feed one config object: a select,
    // a textarea, and — on a webhook — a checkbox that must arrive as a boolean
    // rather than as the string an input would otherwise hand over.
    api.createTrigger.mockResolvedValue(trigger({ kind: "webhook" }));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Webhook/ }));

    await userEvent.click(within(dialog).getByLabelText(/Require a signature/));
    await userEvent.selectOptions(within(dialog).getByLabelText(/Write a/), "changelog");
    await userEvent.type(
      within(dialog).getByLabelText(/Standing instructions/),
      "Keep it under 400 words.",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    await waitFor(() => expect(api.createTrigger).toHaveBeenCalled());
    expect(api.createTrigger.mock.calls[0][0].config).toMatchObject({
      require_signature: true,
      content_type: "changelog",
      instructions: "Keep it under 400 words.",
    });
  });

  it("attaches the trigger to the project chosen, not the first one listed", async () => {
    api.listProjects.mockResolvedValue([
      { id: 7, name: "Herald" },
      { id: 8, name: "Second" },
    ]);
    api.createTrigger.mockResolvedValue(trigger());
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    const dialog = screen.getByRole("dialog");

    await userEvent.selectOptions(within(dialog).getByLabelText(/Project/), "8");
    await userEvent.type(
      within(dialog).getByLabelText(/Feed URL/),
      "https://example.com/f.xml",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Create trigger" }));

    await waitFor(() => expect(api.createTrigger).toHaveBeenCalled());
    expect(api.createTrigger.mock.calls[0][0].project_id).toBe(8);
  });

  it("closes without creating anything on cancel", async () => {
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Add trigger" }));
    await userEvent.type(
      within(screen.getByRole("dialog")).getByLabelText(/Feed URL/),
      "https://example.com/f.xml",
    );

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.createTrigger).not.toHaveBeenCalled();
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

  it("is dismissable, and does not come back once dismissed", async () => {
    // It is the only time the secret is ever readable, so the panel stays put
    // until the user says they have it — and once they have, showing it again
    // on the next render would undo the "shown once" the copy promises.
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
    await screen.findByText("s3cr3t-value");

    await userEvent.click(screen.getByRole("button", { name: /I’ve saved it/ }));

    expect(screen.queryByText("s3cr3t-value")).not.toBeInTheDocument();
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

// ---- The two buttons that take something away ------------------------------
//
// Every test above drives a button whose worst outcome is a wasted request.
// These two are the other kind: `Delete` removes the trigger, and
// `Rotate secret` invalidates a URL that other people's systems are already
// calling. Both sit behind a `window.confirm`, and a confirm nothing asserts is
// a confirm that can quietly stop being there.

/** Make `window.confirm` answer `verdict`, and hand back the spy. */
function confirming(verdict) {
  const spy = vi.fn(() => verdict);
  vi.stubGlobal("confirm", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("deleting a trigger", () => {
  it("asks first, and does nothing at all when the answer is no", async () => {
    const confirm = confirming(false);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(confirm).toHaveBeenCalled();
    expect(api.deleteTrigger).not.toHaveBeenCalled();
  });

  it("says what stops and what is kept, so the answer can be an informed one", async () => {
    // A trigger is a watcher, not a container: deleting it does not delete the
    // drafts it wrote. A prompt that leaves that ambiguous gets answered "no"
    // by people who wanted "yes".
    const confirm = confirming(false);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    const [message] = confirm.mock.calls[0];
    expect(message).toMatch(/Herald stops watching/);
    expect(message).toMatch(/already wrote is kept/);
  });

  it("deletes and reloads once confirmed", async () => {
    confirming(true);
    api.deleteTrigger.mockResolvedValue(undefined);
    draw();
    await screen.findByText("Changelog feed");
    const before = api.listTriggers.mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(api.deleteTrigger).toHaveBeenCalledWith(1));
    expect(toast.success).toHaveBeenCalledWith("Trigger deleted");
    await waitFor(() =>
      expect(api.listTriggers.mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("leaves the row in place when the delete is refused", async () => {
    // The card must not disappear optimistically: a trigger that is still
    // firing while its row is gone is the worst of both.
    confirming(true);
    api.deleteTrigger.mockRejectedValue(new Error("Trigger is still running"));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Trigger is still running"),
    );
    expect(screen.getByText("Changelog feed")).toBeInTheDocument();
  });
});

describe("rotating a webhook's secret", () => {
  const webhook = () =>
    trigger({
      kind: "webhook",
      name: "Deploy hook",
      config: {},
      inbound_url: "https://herald.test/api/v1/triggers/inbound/old",
      has_secret: true,
    });

  it("is offered only for the kind that has an inbound URL to rotate", async () => {
    draw();
    await screen.findByText("Changelog feed");
    expect(screen.queryByRole("button", { name: "Rotate secret" })).not.toBeInTheDocument();

    api.listTriggers.mockResolvedValue([webhook()]);
    draw();
    expect(
      await screen.findByRole("button", { name: "Rotate secret" }),
    ).toBeInTheDocument();
  });

  it("warns that the old URL stops working immediately", async () => {
    // The cost of this one is not on Herald's side: whatever is POSTing to the
    // old URL starts failing the moment it is confirmed, and nobody is told.
    const confirm = confirming(false);
    api.listTriggers.mockResolvedValue([webhook()]);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Rotate secret" }));

    expect(confirm.mock.calls[0][0]).toMatch(/stops working immediately/);
    expect(api.rotateTriggerSecret).not.toHaveBeenCalled();
  });

  it("shows the new credentials once, the same way a new trigger does", async () => {
    // The rotated secret has exactly the same one-chance property as a freshly
    // created one, and reaches the same dialog by a different route.
    confirming(true);
    api.listTriggers.mockResolvedValue([webhook()]);
    api.rotateTriggerSecret.mockResolvedValue({
      ...webhook(),
      inbound_url: "https://herald.test/api/v1/triggers/inbound/new",
      secret: "rotated-s3cr3t",
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Rotate secret" }));

    expect(await screen.findByText("rotated-s3cr3t")).toBeInTheDocument();
    expect(
      screen.getByText("https://herald.test/api/v1/triggers/inbound/new"),
    ).toBeInTheDocument();
    expect(screen.getByText(/never again/)).toBeInTheDocument();
  });

  it("reports a failed rotation rather than showing a half-rotated dialog", async () => {
    confirming(true);
    api.listTriggers.mockResolvedValue([webhook()]);
    api.rotateTriggerSecret.mockRejectedValue(new Error("Encryption key unavailable"));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Rotate secret" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Encryption key unavailable"),
    );
    expect(screen.queryByText(/never again/)).not.toBeInTheDocument();
  });
});

// ---- Copying the things that cannot be read back --------------------------
//
// An inbound URL and a signing secret are both "paste this somewhere else" by
// nature, and the secret is shown exactly once. Clipboard access is denied in
// plenty of ordinary configurations — an insecure origin, a permissions
// policy — and a rejected `writeText` that nothing catches is a Copy button
// that silently does nothing at the one moment the value is still on screen.

describe("copying credentials", () => {
  function clipboard(behaviour) {
    const writeText = vi.fn(behaviour);
    Object.assign(navigator, { clipboard: { writeText } });
    return writeText;
  }

  async function openRotatedDialog() {
    confirming(true);
    api.listTriggers.mockResolvedValue([
      trigger({ kind: "webhook", config: {}, inbound_url: "https://herald.test/old" }),
    ]);
    api.rotateTriggerSecret.mockResolvedValue({
      ...trigger({ kind: "webhook", config: {} }),
      inbound_url: "https://herald.test/new",
      secret: "top-secret",
    });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Rotate secret" }));
    await screen.findByText("top-secret");
    return within(screen.getByRole("dialog"));
  }

  it("copies the URL and the secret separately, naming which one landed", async () => {
    // Two Copy buttons a few lines apart, and the toast is the only thing that
    // says which was pressed.
    const writeText = clipboard(() => Promise.resolve());
    const dialog = await openRotatedDialog();
    const [copyUrl, copySecret] = dialog.getAllByRole("button", { name: "Copy" });

    await userEvent.click(copyUrl);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("URL copied"));
    expect(writeText).toHaveBeenLastCalledWith("https://herald.test/new");

    await userEvent.click(copySecret);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Secret copied"));
    expect(writeText).toHaveBeenLastCalledWith("top-secret");
  });

  it("says the copy failed rather than looking like it worked", async () => {
    clipboard(() => Promise.reject(new Error("Denied")));
    const dialog = await openRotatedDialog();

    await userEvent.click(dialog.getAllByRole("button", { name: "Copy" })[0]);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not copy — select it and copy manually.",
      ),
    );
    // Still on screen, which is what makes "copy it manually" honest advice.
    expect(dialog.getByText("top-secret")).toBeInTheDocument();
  });

  it("copies a listed trigger's inbound URL from the card itself", async () => {
    const writeText = clipboard(() => Promise.resolve());
    api.listTriggers.mockResolvedValue([
      trigger({
        kind: "webhook",
        config: {},
        inbound_url: "https://herald.test/api/v1/triggers/inbound/tok",
      }),
    ]);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("URL copied"));
    expect(writeText).toHaveBeenCalledWith(
      "https://herald.test/api/v1/triggers/inbound/tok",
    );
  });

  it("points at the URL on screen when the card's copy is refused", async () => {
    clipboard(() => Promise.reject(new Error("Denied")));
    api.listTriggers.mockResolvedValue([
      trigger({ kind: "webhook", config: {}, inbound_url: "https://herald.test/tok" }),
    ]);
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not copy — select the URL and copy it manually.",
      ),
    );
  });
});

describe("when an action fails outright", () => {
  it("reports a failed pause instead of leaving the button looking stuck", async () => {
    // `busy` gates the button, and the `finally` that clears it is the only
    // thing between a failed request and a permanently disabled control.
    api.updateTrigger.mockRejectedValue(new Error("Trigger not found"));
    draw();
    const pause = await screen.findByRole("button", { name: "Pause" });
    await userEvent.click(pause);

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Trigger not found"));
    expect(await screen.findByRole("button", { name: "Pause" })).toBeEnabled();
  });

  it("reports a check that could not be run at all", async () => {
    // Distinct from `{ status: "error" }`, which is a check that ran and found
    // a broken feed. This is the request itself failing.
    api.checkTrigger.mockRejectedValue(new Error("Network unreachable"));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Network unreachable"),
    );
    expect(await screen.findByRole("button", { name: "Check now" })).toBeEnabled();
  });
});

describe("editing an existing trigger", () => {
  it("updates rather than creating a second one", async () => {
    // The builder is one component in two modes, and the mode is "was it given
    // a trigger". Getting it wrong leaves the original watching alongside a
    // duplicate.
    api.updateTrigger.mockResolvedValue(trigger({ name: "Renamed feed" }));
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = within(screen.getByRole("dialog"));
    await userEvent.clear(dialog.getByLabelText(/Name/));
    await userEvent.type(dialog.getByLabelText(/Name/), "Renamed feed");
    await userEvent.click(dialog.getByRole("button", { name: /Save/ }));

    await waitFor(() =>
      expect(api.updateTrigger).toHaveBeenCalledWith(1, {
        name: "Renamed feed",
        config: { feed_url: "https://example.com/changelog.rss" },
      }),
    );
    expect(api.createTrigger).not.toHaveBeenCalled();
  });

  it("does not reopen the secret dialog on a plain edit", async () => {
    // An update returns the trigger without a secret — there is no second
    // chance to see one, and a dialog claiming otherwise would be a lie.
    api.updateTrigger.mockResolvedValue(trigger());
    draw();
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = within(screen.getByRole("dialog"));
    await userEvent.click(dialog.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Trigger saved"));
    expect(screen.queryByText(/never again/)).not.toBeInTheDocument();
  });
});
