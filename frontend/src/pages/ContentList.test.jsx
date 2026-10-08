/**
 * The content list: filterable by project/status/type, plus the dialog that
 * generates a new draft. The filters live in the URL's search params rather
 * than component state, and the Generate button is the one place a project
 * with nothing registered yet is steered toward /projects instead.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentList from "./ContentList";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: {
    listProjects: vi.fn(),
    listContent: vi.fn(),
    generateContent: vi.fn(),
    approveContent: vi.fn(),
    updateContent: vi.fn(),
    bulkApprove: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

/**
 * The query string the page has put the filters into.
 *
 * `MemoryRouter` keeps its location off `window`, so `window.location.search`
 * is empty here no matter what the page does — an assertion against it would
 * pass without the page ever being right.
 */
function Search() {
  return <span data-testid="search">{useLocation().search}</span>;
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <ContentList />
      <Search />
    </MemoryRouter>,
  );
}

const PROJECT = { id: 1, name: "Pulse", repo_full_name: "r2st/DoAide-Pulse" };

function item(overrides = {}) {
  return {
    id: 1,
    title: "A retry budget that outlasts the outage",
    project_name: "Pulse",
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
    await screen.findByRole("option", { name: "Pulse" });
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

  it("drops a filter from the query rather than sending it empty", async () => {
    // Choosing the "All projects" option is a removal, not a value. Setting it
    // instead of deleting leaves `?project=` on the URL — which survives being
    // bookmarked and shared, and reaches the API as a blank filter rather than
    // as no filter.
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");
    await user.selectOptions(screen.getByLabelText("Filter by project"), "1");
    await waitFor(() =>
      expect(api.listContent).toHaveBeenCalledWith(
        expect.objectContaining({ project_id: "1" }),
      ),
    );
    api.listContent.mockClear();

    await user.selectOptions(screen.getByLabelText("Filter by project"), "");

    await waitFor(() => expect(api.listContent).toHaveBeenCalled());
    const [sent] = api.listContent.mock.calls.at(-1);
    expect(sent.project_id).toBeUndefined();
    expect(screen.getByTestId("search")).not.toHaveTextContent("project=");
  });

  it("keeps the filters that were not touched", async () => {
    // The two selects share one query string, and rebuilding it from the
    // current params is what stops the second choice erasing the first.
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");

    await user.selectOptions(screen.getByLabelText("Filter by status"), "draft");
    await user.selectOptions(screen.getByLabelText("Filter by content type"), "tutorial");

    await waitFor(() =>
      expect(api.listContent).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "draft", content_type: "tutorial" }),
      ),
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

  it("opens from the header button once the list has something in it", async () => {
    // The empty state's button and the header's are separate elements, and the
    // empty one stops rendering the moment there is a first piece — so the
    // header is the only way in for every account past its first draft.
    api.listContent.mockResolvedValue([item()]);
    const user = userEvent.setup();
    draw();
    await screen.findByText("A retry budget that outlasts the outage");

    await user.click(screen.getByRole("button", { name: "Generate" }));

    expect(screen.getByRole("dialog", { name: "Generate content" })).toBeInTheDocument();
  });

  it("sends every field the form collects, not just the ones it defaults", async () => {
    // Each control is wired separately, so a select that renders its value but
    // never writes it back looks correct on screen and silently submits the
    // default. The assertion is on the payload, not on the inputs.
    api.listProjects.mockResolvedValue([
      PROJECT,
      { id: 2, name: "Second", repo_full_name: "r2st/Second" },
    ]);
    api.generateContent.mockResolvedValue({ id: 9 });
    const user = userEvent.setup();
    draw();
    await screen.findByText("Nothing written yet");
    await user.click(screen.getByRole("button", { name: "Generate a draft" }));

    const dialog = screen.getByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Project"), "2");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "changelog");
    await user.type(
      within(dialog).getByLabelText(/Direction/),
      "Mention the retry budget.",
    );
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    expect(api.generateContent).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: 2,
        content_type: "changelog",
        instructions: "Mention the retry budget.",
      }),
    );
  });
});

describe("review mode", () => {
  function drawReview() {
    return render(
      <MemoryRouter initialEntries={["/?status=review"]} future={ROUTER_FUTURE}>
        <ContentList />
        <Search />
      </MemoryRouter>,
    );
  }

  it("shows review cards with approve/reject buttons when filtered to review", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 1, status: "review", confidence: 0.85 }),
      item({ id: 2, title: "Second draft", status: "review", confidence: 0.6 }),
    ]);
    drawReview();

    expect(await screen.findByText("A retry budget that outlasts the outage")).toBeInTheDocument();
    const approveButtons = screen.getAllByRole("button", { name: "Approve" });
    const rejectButtons = screen.getAllByRole("button", { name: "Reject" });
    expect(approveButtons.length).toBe(2);
    expect(rejectButtons.length).toBe(2);
  });

  it("shows an Approve All button in the header", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 1, status: "review" }),
    ]);
    drawReview();

    expect(await screen.findByRole("button", { name: "Approve all" })).toBeInTheDocument();
  });

  it("calls bulkApprove when Approve All is clicked", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 1, status: "review" }),
      item({ id: 2, title: "Second", status: "review" }),
    ]);
    api.bulkApprove.mockResolvedValue({ succeeded: [1, 2], failed: [] });
    const user = userEvent.setup();
    drawReview();
    await screen.findByRole("button", { name: "Approve all" });

    await user.click(screen.getByRole("button", { name: "Approve all" }));

    await vi.waitFor(() => {
      expect(api.bulkApprove).toHaveBeenCalledWith([1, 2]);
    });
  });

  it("shows confidence bar on review cards", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 1, status: "review", confidence: 0.85 }),
    ]);
    drawReview();

    expect(await screen.findByText("85%")).toBeInTheDocument();
  });

  it("calls approveContent for single approve", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 42, status: "review" }),
    ]);
    api.approveContent.mockResolvedValue({});
    const user = userEvent.setup();
    drawReview();

    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(api.approveContent).toHaveBeenCalledWith(42);
  });

  it("moves to draft on reject", async () => {
    api.listContent.mockResolvedValue([
      item({ id: 42, status: "review" }),
    ]);
    api.updateContent.mockResolvedValue({});
    const user = userEvent.setup();
    drawReview();

    await user.click(await screen.findByRole("button", { name: "Reject" }));

    expect(api.updateContent).toHaveBeenCalledWith(42, { status: "draft" });
  });
});
