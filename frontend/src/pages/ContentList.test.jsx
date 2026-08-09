/**
 * The content list: filterable by project/status/type, plus the dialog that
 * generates a new draft. The filters live in the URL's search params rather
 * than component state, and the Generate button is the one place a project
 * with nothing registered yet is steered toward /projects instead.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentList from "./ContentList";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: { listProjects: vi.fn(), listContent: vi.fn(), generateContent: vi.fn() },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

function draw() {
  return render(
    <MemoryRouter>
      <ContentList />
    </MemoryRouter>,
  );
}

const PROJECT = { id: 1, name: "Herald", repo_full_name: "r2st/Herald" };

function item(overrides = {}) {
  return {
    id: 1,
    title: "A retry budget that outlasts the outage",
    project_name: "Herald",
    content_type: "changelog",
    word_count: 812,
    read_minutes: 4,
    created_at: "2026-08-01T10:00:00Z",
    status: "draft",
    confidence: null,
    publications: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listProjects.mockResolvedValue([PROJECT]);
  api.listContent.mockResolvedValue([]);
});

describe("loading and empty states", () => {
  it("shows a skeleton before the first response", async () => {
    api.listContent.mockReturnValue(new Promise(() => {}));
    draw();

    expect(screen.getByText("Content")).toBeInTheDocument();
    // Let the (unrelated) project list finish loading within `act`.
    await screen.findByRole("option", { name: "Herald" });
  });

  it("offers to register a project when there are none at all", async () => {
    api.listProjects.mockResolvedValue([]);
    draw();

    expect(await screen.findByText("Nothing written yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Register a project" })).toHaveAttribute(
      "href",
      "/projects",
    );
  });

  it("offers to generate a draft when a project exists but nothing is written", async () => {
    draw();

    expect(await screen.findByText("Nothing written yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate a draft" })).toBeInTheDocument();
  });

  it("says nothing matches, distinct from nothing written, once a filter is applied", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");

    await user.selectOptions(screen.getByLabelText("Filter by status"), "published");

    expect(await screen.findByText("Nothing matches")).toBeInTheDocument();
    expect(screen.getByText("Try clearing a filter.")).toBeInTheDocument();
  });
});

describe("the generate button", () => {
  it("is disabled with no projects registered", async () => {
    api.listProjects.mockResolvedValue([]);
    draw();
    await screen.findByText("Nothing written yet");

    expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("is enabled once a project exists", async () => {
    draw();
    await screen.findByText("Nothing written yet");

    expect(screen.getByRole("button", { name: "Generate" })).toBeEnabled();
  });
});

describe("filters", () => {
  it("re-fetches with the chosen project", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");
    api.listContent.mockClear();

    await user.selectOptions(screen.getByLabelText("Filter by project"), "1");

    expect(api.listContent).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: "1" }),
    );
  });

  it("re-fetches with the chosen content type", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");
    api.listContent.mockClear();

    await user.selectOptions(screen.getByLabelText("Filter by content type"), "tutorial");

    expect(api.listContent).toHaveBeenCalledWith(
      expect.objectContaining({ content_type: "tutorial" }),
    );
  });
});

describe("the list", () => {
  it("renders a piece's metadata and status", async () => {
    api.listContent.mockResolvedValue([item()]);
    draw();

    expect(
      await screen.findByText("A retry budget that outlasts the outage"),
    ).toBeInTheDocument();
    expect(screen.getByText("812 words")).toBeInTheDocument();
    expect(screen.getByText("4 min read")).toBeInTheDocument();
  });

  it("shows a chip per publication", async () => {
    api.listContent.mockResolvedValue([
      item({
        publications: [{ id: 1, platform: "devto", status: "published" }],
      }),
    ]);
    draw();

    const row = (await screen.findByText("A retry budget that outlasts the outage")).closest(
      "li",
    );
    expect(within(row).getByText("Devto · published")).toBeInTheDocument();
  });

  it("shows no confidence indicator for a piece that never went through the model", async () => {
    api.listContent.mockResolvedValue([item({ confidence: null })]);
    draw();

    await screen.findByText("A retry budget that outlasts the outage");
    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
  });
});

describe("generating a draft", () => {
  it("opens a dialog scoped to the current project filter", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");

    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    expect(screen.getByRole("dialog", { name: "Generate content" })).toBeInTheDocument();
  });

  it("submits the form and reloads the list on success", async () => {
    const user = userEvent.setup();
    api.generateContent.mockResolvedValue({ id: 9 });
    draw();
    await screen.findByText("Nothing written yet");
    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    expect(api.generateContent).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: 1, content_type: "feature_spotlight" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("toasts the error and leaves the dialog open on failure", async () => {
    const user = userEvent.setup();
    api.generateContent.mockRejectedValue(new Error("Model unavailable"));
    draw();
    await screen.findByText("Nothing written yet");
    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith("Model unavailable");
  });

  it("offers to pull repo activity only when the project has a repo", async () => {
    api.listProjects.mockResolvedValue([{ id: 2, name: "No Repo" }]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");

    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    expect(screen.queryByText(/Pull recent commits/)).not.toBeInTheDocument();
  });

  it("closes without saving on cancel", async () => {
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");
    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.generateContent).not.toHaveBeenCalled();
  });
});
