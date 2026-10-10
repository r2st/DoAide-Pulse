/**
 * The Settings page's service-health panel.
 *
 * Every row in it — the provider chain, the open breakers, the GitHub token,
 * credential encryption — is a field only `GET /health/detail` returns. The
 * public `/health` is a liveness probe and deliberately withholds all of them,
 * so a page that asked it instead rendered a fully configured server as "none
 * configured", "anonymous" and "off": three rows telling the operator to go and
 * set environment variables that were already set.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    platforms: vi.fn(),
    healthDetail: vi.fn(),
    health: vi.fn(),
    verifyConnection: vi.fn(),
    deleteConnection: vi.fn(),
    saveConnection: vi.fn(),
    listWebhooks: vi.fn(),
    webhookEvents: vi.fn(),
    createWebhook: vi.fn(),
    deleteWebhook: vi.fn(),
    updateWebhook: vi.fn(),
    pingWebhook: vi.fn(),
    listApiKeys: vi.fn(),
    apiKeyScopes: vi.fn(),
    createApiKey: vi.fn(),
    revokeApiKey: vi.fn(),
    listProjects: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

// One object, not a fresh `{refresh: vi.fn()}` per call: the page calls
// `refresh()` to re-read the user after a connection changes, and a mock that
// is rebuilt on every render can never be asserted against.
const auth = { user: { email: "you@example.com" }, refresh: vi.fn() };
vi.mock("../hooks/useAuth", () => ({ useAuth: () => auth }));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Settings />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.platforms.mockResolvedValue([]);
  api.healthDetail.mockResolvedValue({
    status: "ok",
    database: { status: "ok", required: true, detail: "" },
    redis: { status: "ok", required: false, detail: "" },
    llm_providers: ["openrouter", "groq"],
    llm_breakers_open: {},
    github_configured: true,
    credential_encryption: true,
    implemented_platforms: ["devto"],
  });
  api.listWebhooks.mockResolvedValue([]);
  api.webhookEvents.mockResolvedValue([]);
  api.listApiKeys.mockResolvedValue([]);
  api.apiKeyScopes.mockResolvedValue([]);
  api.listProjects.mockResolvedValue([]);
});

describe("service health", () => {
  it("reads the authenticated detail endpoint, not the public probe", async () => {
    draw();
    await screen.findByText("openrouter → groq");

    expect(api.healthDetail).toHaveBeenCalled();
    expect(api.health).not.toHaveBeenCalled();
  });

  it("reports a configured server as configured", async () => {
    draw();

    expect(await screen.findByText("openrouter → groq")).toBeInTheDocument();
    expect(screen.getByText("token set")).toBeInTheDocument();
    expect(screen.getByText("on")).toBeInTheDocument();
  });

  it("still reports an unconfigured one honestly", async () => {
    api.healthDetail.mockResolvedValue({
      status: "ok",
      llm_providers: [],
      llm_breakers_open: {},
      github_configured: false,
      credential_encryption: false,
    });
    draw();

    expect(await screen.findByText("none configured")).toBeInTheDocument();
    expect(screen.getByText("anonymous")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
  });

  it("names the providers being skipped while their cool-down runs", async () => {
    api.healthDetail.mockResolvedValue({
      status: "ok",
      llm_providers: ["openrouter", "groq"],
      llm_breakers_open: { groq: { failures: 0, seconds_until_retry: 240 } },
      github_configured: true,
      credential_encryption: true,
    });
    draw();

    expect(await screen.findByText("groq")).toBeInTheDocument();
  });
});

/**
 * The other two thirds of this page: the platform rows and the dialog that
 * connects one.
 *
 * Both are entirely about a credential the API will never hand back. A row
 * cannot show you the key it holds, so what it *can* show — connected or
 * needing a reconnect, which handle it authenticated as, what the platform
 * said when it last refused — is the whole of the operator's read on whether
 * publishing will work tonight. And "Connect" on a platform Pulse has no
 * adapter for, or a Disconnect that goes through without a confirm, are both
 * failures the health panel above says nothing about.
 */

/** A platform capability row as `GET /settings/platforms` returns one. */
function platform(overrides = {}) {
  return {
    platform: "devto",
    display_name: "Dev.to",
    implemented: true,
    supports_metrics: true,
    caveat: "",
    credential_fields: [
      { key: "api_key", label: "API key", help_text: "", secret: true, required: true },
    ],
    connection: null,
    ...overrides,
  };
}

/** The connection sub-object, present only once a key has been saved. */
function connection(overrides = {}) {
  return {
    id: 1,
    platform: "devto",
    status: "connected",
    display_name: "@you",
    last_verified_at: null,
    last_error: null,
    ...overrides,
  };
}

describe("platform rows", () => {
  it("offers no Connect button for an adapter that is not built", async () => {
    api.platforms.mockResolvedValue([
      platform({ platform: "medium", display_name: "Medium", implemented: false }),
    ]);
    draw();

    const button = await screen.findByRole("button", { name: "Connect" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "This adapter is not finished yet");
    expect(screen.getByText("not built")).toBeInTheDocument();
  });

  it("leaves Connect live on one that is", async () => {
    api.platforms.mockResolvedValue([platform()]);
    draw();

    expect(await screen.findByRole("button", { name: "Connect" })).toBeEnabled();
    expect(screen.queryByText("not built")).not.toBeInTheDocument();
  });

  it("shows Verify and Disconnect only once a key is stored", async () => {
    api.platforms.mockResolvedValue([platform()]);
    draw();
    await screen.findByRole("button", { name: "Connect" });

    expect(screen.queryByRole("button", { name: "Verify" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Disconnect" }),
    ).not.toBeInTheDocument();
  });

  it("calls the second key a replacement, because the first is unreadable", async () => {
    api.platforms.mockResolvedValue([platform({ connection: connection() })]);
    draw();

    expect(await screen.findByRole("button", { name: "Replace key" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Verify" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
  });

  it("badges a connection the platform has stopped accepting as needing a reconnect", async () => {
    api.platforms.mockResolvedValue([
      platform({
        connection: connection({ status: "invalid", last_error: "401 Unauthorized" }),
      }),
    ]);
    draw();

    expect(await screen.findByText("reconnect")).toBeInTheDocument();
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
    expect(screen.getByText("401 Unauthorized")).toBeInTheDocument();
  });

  it("names the handle the stored key authenticated as", async () => {
    api.platforms.mockResolvedValue([platform({ connection: connection() })]);
    draw();

    expect(await screen.findByText(/@you/)).toBeInTheDocument();
  });

  it("says when the key was last known to work, and stays quiet when never", async () => {
    // A stored key is only evidence that something was pasted in; the verified
    // time is the evidence it still opens the door. Saying nothing is right for
    // a key nobody has checked — a blank date would look like a bug.
    api.platforms.mockResolvedValue([
      platform({ connection: connection({ last_verified_at: "2026-08-13T09:00:00Z" }) }),
    ]);
    const { unmount } = draw();

    expect(await screen.findByText(/@you · verified /)).toBeInTheDocument();
    unmount();

    api.platforms.mockResolvedValue([platform({ connection: connection() })]);
    draw();

    // Scoped to the handle line — the panel's own subtitle also says
    // "verified", about when keys are checked rather than about this one.
    expect(await screen.findByText("@you")).toHaveTextContent(/^@you$/);
  });

  it("says so when a platform will never report views", async () => {
    api.platforms.mockResolvedValue([platform({ supports_metrics: false })]);
    draw();

    expect(await screen.findByText(/No stats API/)).toBeInTheDocument();
  });

  it("does not promise stats about an adapter that does not exist", async () => {
    api.platforms.mockResolvedValue([
      platform({ supports_metrics: false, implemented: false }),
    ]);
    draw();

    await screen.findByText("not built");
    expect(screen.queryByText(/No stats API/)).not.toBeInTheDocument();
  });

  it("surfaces the adapter's caveat", async () => {
    api.platforms.mockResolvedValue([
      platform({ caveat: "Medium's API is write-only and deprecated." }),
    ]);
    draw();

    expect(
      await screen.findByText("Medium's API is write-only and deprecated."),
    ).toBeInTheDocument();
  });
});

describe("verifying a stored key", () => {
  beforeEach(() => {
    api.platforms.mockResolvedValue([platform({ connection: connection() })]);
  });

  it("reports a key the platform still accepts", async () => {
    api.verifyConnection.mockResolvedValue({ status: "connected" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    expect(api.verifyConnection).toHaveBeenCalledWith("devto");
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Dev.to is fine"));
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("re-reads the row afterwards, so a status that changed is the one shown", async () => {
    api.verifyConnection.mockResolvedValue({ status: "connected" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    // Once on mount, once because verifying may have changed what it says.
    await waitFor(() => expect(api.platforms).toHaveBeenCalledTimes(2));
    expect(auth.refresh).toHaveBeenCalled();
  });

  it("shows what the platform said when it refuses", async () => {
    api.verifyConnection.mockResolvedValue({
      status: "invalid",
      last_error: "403 Forbidden",
    });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("403 Forbidden"));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("still says something when it refuses without a reason", async () => {
    api.verifyConnection.mockResolvedValue({ status: "invalid", last_error: null });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Credentials were rejected"),
    );
  });

  it("does not reload on a request that never reached the server", async () => {
    api.verifyConnection.mockRejectedValue(new Error("Gateway Timeout"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Gateway Timeout"));
    expect(api.platforms).toHaveBeenCalledTimes(1);
  });

  it("frees the buttons again after a failure, so the row is not stuck", async () => {
    api.verifyConnection.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Verify" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Verify" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  });
});

describe("disconnecting", () => {
  beforeEach(() => {
    api.platforms.mockResolvedValue([platform({ connection: connection() })]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks before throwing a credential away", async () => {
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal("confirm", confirm);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));

    expect(confirm).toHaveBeenCalledWith("Disconnect Dev.to?");
    expect(api.deleteConnection).not.toHaveBeenCalled();
  });

  it("deletes the connection once that is answered", async () => {
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    api.deleteConnection.mockResolvedValue(null);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));

    expect(api.deleteConnection).toHaveBeenCalledWith("devto");
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Disconnected"));
    await waitFor(() => expect(api.platforms).toHaveBeenCalledTimes(2));
  });

  it("keeps the row when the delete fails", async () => {
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    api.deleteConnection.mockRejectedValue(new Error("Conflict"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Conflict"));
    expect(api.platforms).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  });
});

describe("the connect dialog", () => {
  /**
   * The dialog's own submit.
   *
   * Scoped to the dialog on purpose: the row that opened it is still mounted
   * behind the backdrop with a "Connect" button of its own, so an unscoped
   * query matches two buttons and the test reads as a component bug.
   */
  function submitButton() {
    return within(screen.getByRole("dialog")).getByRole("button", { name: "Connect" });
  }

  const twoFields = [
    { key: "api_key", label: "API key", help_text: "Find it under Settings → Account", secret: true, required: true },
    { key: "org_id", label: "Organisation", help_text: "", secret: false, required: false },
  ];

  it("renders one input per credential field the adapter declares", async () => {
    api.platforms.mockResolvedValue([platform({ credential_fields: twoFields })]);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText(/API key/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Organisation/)).toBeInTheDocument();
    expect(
      within(dialog).getByText("Find it under Settings → Account"),
    ).toBeInTheDocument();
  });

  it("masks a secret field and leaves a non-secret one readable", async () => {
    api.platforms.mockResolvedValue([platform({ credential_fields: twoFields })]);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));

    expect(screen.getByLabelText(/API key/)).toHaveAttribute("type", "password");
    expect(screen.getByLabelText(/Organisation/)).toHaveAttribute("type", "text");
  });

  it("marks the optional field optional and requires the rest", async () => {
    api.platforms.mockResolvedValue([platform({ credential_fields: twoFields })]);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));

    expect(screen.getByLabelText(/Organisation \(optional\)/)).not.toBeRequired();
    expect(screen.getByLabelText(/API key/)).toBeRequired();
  });

  it("sends what was typed and saves nothing for the blank optional field", async () => {
    api.platforms.mockResolvedValue([platform({ credential_fields: twoFields })]);
    api.saveConnection.mockResolvedValue({ status: "connected" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.type(screen.getByLabelText(/API key/), "secret-key");
    await user.click(submitButton());

    await waitFor(() =>
      expect(api.saveConnection).toHaveBeenCalledWith("devto", {
        api_key: "secret-key",
      }),
    );
  });

  it("treats a field holding only spaces as unfilled", async () => {
    api.platforms.mockResolvedValue([platform({ credential_fields: twoFields })]);
    api.saveConnection.mockResolvedValue({ status: "connected" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.type(screen.getByLabelText(/API key/), "secret-key");
    await user.type(screen.getByLabelText(/Organisation/), "   ");
    await user.click(submitButton());

    await waitFor(() =>
      expect(api.saveConnection).toHaveBeenCalledWith("devto", {
        api_key: "secret-key",
      }),
    );
  });

  it("closes and re-reads the platforms once the key verifies", async () => {
    api.platforms.mockResolvedValue([platform()]);
    api.saveConnection.mockResolvedValue({ status: "connected" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.type(screen.getByLabelText(/API key/), "k");
    await user.click(submitButton());

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Dev.to connected"),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.platforms).toHaveBeenCalledTimes(2);
    expect(auth.refresh).toHaveBeenCalled();
  });

  it("stays open with the typing intact when the platform rejects the key", async () => {
    api.platforms.mockResolvedValue([platform()]);
    api.saveConnection.mockRejectedValue(new Error("Credentials were rejected"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.type(screen.getByLabelText(/API key/), "wrong");
    await user.click(submitButton());

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Credentials were rejected"),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/API key/)).toHaveValue("wrong");
    // Re-submittable: a rejected key is the case where you retype it.
    expect(submitButton()).toBeEnabled();
  });

  it("does not reload the page's data when the save failed", async () => {
    api.platforms.mockResolvedValue([platform()]);
    api.saveConnection.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.type(screen.getByLabelText(/API key/), "wrong");
    await user.click(submitButton());

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(api.platforms).toHaveBeenCalledTimes(1);
  });

  it("discards the dialog on Cancel without saving anything", async () => {
    api.platforms.mockResolvedValue([platform()]);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.saveConnection).not.toHaveBeenCalled();
  });

  it("repeats the caveat where the key is actually being entered", async () => {
    api.platforms.mockResolvedValue([
      platform({ caveat: "Posts land as drafts; you publish them by hand." }),
    ]);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Connect" }));

    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText("Posts land as drafts; you publish them by hand."),
    ).toBeInTheDocument();
  });
});

/* ---- Outbound webhooks ---- */

function webhook(overrides = {}) {
  return {
    id: 1,
    url: "https://example.com/hook",
    description: "CI notifier",
    events: ["content_published"],
    is_active: true,
    consecutive_failures: 0,
    last_delivery_at: null,
    last_status: null,
    last_error: null,
    created_at: "2025-01-15T10:00:00Z",
    ...overrides,
  };
}

describe("outbound webhooks", () => {
  it("shows the empty state when there are none", async () => {
    api.listWebhooks.mockResolvedValue([]);
    draw();

    expect(await screen.findByText("No webhooks")).toBeInTheDocument();
  });

  it("renders a webhook row with its URL and events", async () => {
    api.listWebhooks.mockResolvedValue([webhook()]);
    draw();

    expect(await screen.findByText("https://example.com/hook")).toBeInTheDocument();
    expect(screen.getByText("Content Published")).toBeInTheDocument();
    expect(screen.getByText("CI notifier")).toBeInTheDocument();
  });

  it("shows a paused badge on an inactive webhook", async () => {
    api.listWebhooks.mockResolvedValue([webhook({ is_active: false })]);
    draw();

    expect(await screen.findByText("paused")).toBeInTheDocument();
  });

  it("shows a failure badge when consecutive failures > 0", async () => {
    api.listWebhooks.mockResolvedValue([webhook({ consecutive_failures: 3 })]);
    draw();

    expect(await screen.findByText("3 failures")).toBeInTheDocument();
  });

  it("shows the last error when present", async () => {
    api.listWebhooks.mockResolvedValue([
      webhook({ last_error: "Connection refused" }),
    ]);
    draw();

    expect(await screen.findByText("Connection refused")).toBeInTheDocument();
  });

  it("pings a webhook and shows success", async () => {
    api.listWebhooks.mockResolvedValue([webhook()]);
    api.pingWebhook.mockResolvedValue({ status: "delivered" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Ping" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Ping delivered"),
    );
  });

  it("pings a webhook and shows failure", async () => {
    api.listWebhooks.mockResolvedValue([webhook()]);
    api.pingWebhook.mockResolvedValue({ status: "failed", error: "timeout" });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Ping" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("timeout"));
  });

  it("pauses and resumes a webhook", async () => {
    api.listWebhooks.mockResolvedValue([webhook()]);
    api.updateWebhook.mockResolvedValue({});
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(api.updateWebhook).toHaveBeenCalledWith(1, { is_active: false }),
    );
    expect(toast.success).toHaveBeenCalledWith("Paused");
  });

  it("deletes a webhook after confirmation", async () => {
    api.listWebhooks.mockResolvedValue([webhook()]);
    api.deleteWebhook.mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteWebhook).toHaveBeenCalledWith(1));
    expect(toast.success).toHaveBeenCalledWith("Webhook deleted");
    window.confirm.mockRestore();
  });

  it("opens the create dialog and submits a new webhook", async () => {
    api.listWebhooks.mockResolvedValue([]);
    api.webhookEvents.mockResolvedValue([
      { event: "content_published", description: "A piece went live." },
      { event: "publication_failed", description: "A platform gave up." },
    ]);
    api.createWebhook.mockResolvedValue({
      ...webhook(),
      secret: "whsec_test123",
    });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Add webhook" }));

    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Endpoint URL"), "https://hook.test/cb");
    await user.click(within(dialog).getByText("Content Published"));
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.createWebhook).toHaveBeenCalledWith({
        url: "https://hook.test/cb",
        events: ["content_published"],
        description: "",
      }),
    );

    // The one-time secret dialog should appear
    expect(await screen.findByText("Signing secret")).toBeInTheDocument();
    expect(screen.getByDisplayValue("whsec_test123")).toBeInTheDocument();
  });
});

/* ---- API keys ---- */

function apiKey(overrides = {}) {
  return {
    id: 1,
    project_id: 1,
    name: "CI deploy key",
    prefix: "psk_abc",
    scopes: ["content_read"],
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    rotated_from_id: null,
    created_at: "2025-01-20T10:00:00Z",
    ...overrides,
  };
}

describe("API keys", () => {
  it("shows the empty state when there are none", async () => {
    api.listApiKeys.mockResolvedValue([]);
    draw();

    expect(await screen.findByText("No API keys")).toBeInTheDocument();
  });

  it("renders a key row with its name, prefix, and scopes", async () => {
    api.listApiKeys.mockResolvedValue([apiKey()]);
    draw();

    expect(await screen.findByText("CI deploy key")).toBeInTheDocument();
    expect(screen.getByText("psk_abc…")).toBeInTheDocument();
    expect(screen.getByText("Content Read")).toBeInTheDocument();
  });

  it("shows a revoked badge on a revoked key", async () => {
    api.listApiKeys.mockResolvedValue([
      apiKey({ revoked_at: "2025-02-01T10:00:00Z" }),
    ]);
    draw();

    expect(await screen.findByText("revoked")).toBeInTheDocument();
    // No revoke button for an already-revoked key
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("revokes a key after confirmation", async () => {
    api.listApiKeys.mockResolvedValue([apiKey()]);
    api.revokeApiKey.mockResolvedValue({ ...apiKey(), revoked_at: "2025-02-01T10:00:00Z" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(api.revokeApiKey).toHaveBeenCalledWith(1));
    expect(toast.success).toHaveBeenCalledWith("Key revoked");
    window.confirm.mockRestore();
  });

  it("opens the create dialog and mints a key", async () => {
    api.listApiKeys.mockResolvedValue([]);
    api.listProjects.mockResolvedValue([{ id: 1, name: "My Project" }]);
    api.apiKeyScopes.mockResolvedValue([
      { scope: "content_read", description: "List the project's pieces." },
      { scope: "content_write", description: "File an idea." },
    ]);
    api.createApiKey.mockResolvedValue({
      ...apiKey(),
      token: "psk_abc.secret_token_value",
    });
    const user = userEvent.setup();
    draw();

    await user.click(await screen.findByRole("button", { name: "Create key" }));

    const dialog = screen.getByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Project"), "1");
    await user.type(within(dialog).getByLabelText("Key name"), "CI deploy key");
    await user.click(within(dialog).getByText("Content Read"));
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.createApiKey).toHaveBeenCalledWith({
        project_id: 1,
        name: "CI deploy key",
        scopes: ["content_read"],
        expires_in_days: null,
      }),
    );

    // The one-time token dialog should appear
    expect(await screen.findByText("API token")).toBeInTheDocument();
    expect(screen.getByDisplayValue("psk_abc.secret_token_value")).toBeInTheDocument();
  });
});
