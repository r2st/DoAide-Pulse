/**
 * The project registry: cards for what's registered, a dialog to add or edit
 * one, and the two per-card actions — scan and delete — that talk to the API
 * directly rather than through the dialog.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Projects from "./Projects";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    listProjects: vi.fn(),
    createProject: vi.fn(),
    updateProject: vi.fn(),
    deleteProject: vi.fn(),
    scanProject: vi.fn(),
    platforms: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Projects />
    </MemoryRouter>,
  );
}

function project(overrides = {}) {
  return {
    id: 1,
    name: "Herald",
    description: "A dev-blog autopilot.",
    repo_url: "https://github.com/r2st/Herald",
    repo_full_name: "r2st/Herald",
    live_url: "",
    tech_stack: ["FastAPI", "React"],
    target_audience: "",
    keywords: [],
    tone: "technical",
    autopilot_mode: "draft",
    auto_canonical: true,
    canonical_platform: "",
    utm_enabled: true,
    utm_campaign: "",
    slug: "herald",
    content_count: 4,
    published_count: 3,
    last_scanned_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listProjects.mockResolvedValue([]);
  api.platforms.mockResolvedValue([
    { platform: "devto", display_name: "Dev.to", implemented: true },
    { platform: "twitter", display_name: "Twitter / X", implemented: false },
  ]);
});

describe("loading, error and empty states", () => {
  it("shows a skeleton before the first response", () => {
    api.listProjects.mockReturnValue(new Promise(() => {}));
    draw();

    expect(screen.getByText("Projects")).toBeInTheDocument();
  });

  it("shows a retryable error on a failed fetch", async () => {
    api.listProjects.mockRejectedValue(new Error("Bad Gateway"));
    draw();

    expect(await screen.findByText("Bad Gateway")).toBeInTheDocument();
  });

  it("invites registering the first project", async () => {
    draw();

    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add your first project" })).toBeInTheDocument();
  });
});

describe("a project card", () => {
  it("shows its name, links and autopilot mode", async () => {
    api.listProjects.mockResolvedValue([project()]);
    draw();

    expect(await screen.findByText("Herald")).toBeInTheDocument();
    expect(screen.getByText("r2st/Herald")).toBeInTheDocument();
    expect(screen.getByTitle("Autopilot mode")).toHaveTextContent("draft");
  });

  it("falls back to the live URL when there is no repo", async () => {
    api.listProjects.mockResolvedValue([
      project({ repo_full_name: "", live_url: "https://herald.example.com" }),
    ]);
    draw();

    expect(await screen.findByText("https://herald.example.com")).toBeInTheDocument();
  });

  it("offers a scan button only when a repo is connected", async () => {
    api.listProjects.mockResolvedValue([project({ repo_full_name: "" })]);
    draw();

    await screen.findByText("Herald");
    expect(screen.queryByRole("button", { name: "Scan repo" })).not.toBeInTheDocument();
  });

  it("shows content counts", async () => {
    api.listProjects.mockResolvedValue([project()]);
    draw();

    await screen.findByText("Herald");
    expect(screen.getByText("4 pieces")).toBeInTheDocument();
    expect(screen.getByText("3 published")).toBeInTheDocument();
  });
});

describe("scanning a repo", () => {
  it("reports what the scan found and reloads", async () => {
    api.listProjects.mockResolvedValue([project()]);
    api.scanProject.mockResolvedValue({
      full_name: "r2st/Herald",
      new_commit_count: 3,
      new_release_tag: null,
    });
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Scan repo" }));

    await vi.waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("r2st/Herald: 3 new commit(s)"),
    );
    expect(api.scanProject).toHaveBeenCalledWith(1);
    expect(api.listProjects).toHaveBeenCalledTimes(2);
  });

  it("mentions the release tag alongside the commit count when one shipped", async () => {
    api.listProjects.mockResolvedValue([project()]);
    api.scanProject.mockResolvedValue({
      full_name: "r2st/Herald",
      new_commit_count: 5,
      new_release_tag: "v2.1.0",
    });
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Scan repo" }));

    await vi.waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "r2st/Herald: 5 new commit(s), release v2.1.0",
      ),
    );
  });

  it("toasts the error rather than throwing", async () => {
    api.listProjects.mockResolvedValue([project()]);
    api.scanProject.mockRejectedValue(new Error("rate limited"));
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Scan repo" }));

    expect(await screen.findByRole("button", { name: "Scan repo" })).toBeEnabled();
    expect(toast.error).toHaveBeenCalledWith("rate limited");
  });
});

describe("deleting a project", () => {
  let confirmSpy;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it("asks for confirmation naming the project before deleting, then reloads", async () => {
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.listProjects.mockResolvedValueOnce([project()]).mockResolvedValueOnce([]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Herald"));
    expect(api.deleteProject).toHaveBeenCalledWith(1);
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
  });

  it("does nothing when the confirmation is declined", async () => {
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    api.listProjects.mockResolvedValue([project()]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(api.deleteProject).not.toHaveBeenCalled();
    expect(screen.getByText("Herald")).toBeInTheDocument();
  });
});

describe("the add/edit dialog", () => {
  it("opens empty from Add project", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");

    await user.click(screen.getByRole("button", { name: "Add your first project" }));

    expect(screen.getByRole("heading", { name: "Add a project" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("");
  });

  // The dialog's own keyboard behaviour is covered in ui/Dialog.test.jsx. What
  // these two check is that this page is wired to it at all — the eight dialogs
  // this replaced each had `aria-modal` and none of them behaved like it.
  it("puts focus in the first field, so it can be filled in without a mouse", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");

    await user.click(screen.getByRole("button", { name: "Add your first project" }));

    expect(screen.getByLabelText("Name")).toHaveFocus();
  });

  it("closes on Escape and hands focus back to what opened it", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");
    const opener = screen.getByRole("button", { name: "Add your first project" });
    await user.click(opener);

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("pre-fills from an existing project when editing", async () => {
    api.listProjects.mockResolvedValue([project()]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");

    await user.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByRole("heading", { name: "Edit Herald" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("Herald");
    expect(screen.getByLabelText(/Tech stack/)).toHaveValue("FastAPI, React");
  });

  it("only lists implemented platforms as canonical destinations", async () => {
    api.listProjects.mockResolvedValue([project()]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const select = await screen.findByLabelText("Primary destination");
    expect(within(select).getByText("Dev.to")).toBeInTheDocument();
    expect(within(select).queryByText("Twitter / X")).not.toBeInTheDocument();
  });

  it("disables the campaign name field until UTM tagging is on", async () => {
    api.listProjects.mockResolvedValue([project({ utm_enabled: false })]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("Campaign name")).toBeDisabled();
  });

  it("creates a project, splitting comma-separated lists and nulling blank URLs", async () => {
    api.createProject.mockResolvedValue(project());
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");
    await user.click(screen.getByRole("button", { name: "Add your first project" }));

    await user.type(screen.getByLabelText("Name"), "New Project");
    await user.type(screen.getByLabelText(/Tech stack/), "Go, Postgres");
    await user.click(screen.getByRole("button", { name: "Save project" }));

    expect(api.createProject).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "New Project",
        tech_stack: ["Go", "Postgres"],
        repo_url: null,
        live_url: null,
        canonical_platform: null,
      }),
    );
  });

  it("updates an existing project instead of creating a new one", async () => {
    api.listProjects.mockResolvedValue([project()]);
    api.updateProject.mockResolvedValue(project());
    const user = userEvent.setup();
    draw();
    await screen.findByText("Herald");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    await user.click(screen.getByRole("button", { name: "Save project" }));

    expect(api.updateProject).toHaveBeenCalledWith(1, expect.any(Object));
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("toasts success and closes the dialog after saving", async () => {
    api.createProject.mockResolvedValue(project());
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");
    await user.click(screen.getByRole("button", { name: "Add your first project" }));
    await user.type(screen.getByLabelText("Name"), "New Project");

    await user.click(screen.getByRole("button", { name: "Save project" }));

    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Project saved");
  });

  it("keeps the dialog open and toasts the error on a failed save", async () => {
    api.createProject.mockRejectedValue(new Error("Name already in use"));
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");
    await user.click(screen.getByRole("button", { name: "Add your first project" }));
    await user.type(screen.getByLabelText("Name"), "New Project");

    await user.click(screen.getByRole("button", { name: "Save project" }));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith("Name already in use");
  });

  it("closes without saving on cancel", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("No projects yet");
    await user.click(screen.getByRole("button", { name: "Add your first project" }));

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });
});
