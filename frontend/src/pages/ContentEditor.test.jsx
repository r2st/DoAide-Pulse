import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentEditor from "./ContentEditor";
import { api } from "../lib/api";
import * as draftStore from "../lib/draftStore";

vi.mock("../lib/api", () => ({
  api: {
    getContent: vi.fn(),
    platforms: vi.fn(),
    updateContent: vi.fn(),
    approveContent: vi.fn(),
    deleteContent: vi.fn(),
    checkLinks: vi.fn(),
    socialCards: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

function content(overrides = {}) {
  return {
    id: 3,
    project_id: 7,
    project_name: "Herald",
    title: "Saved title",
    body_markdown: "Saved body",
    excerpt: "Saved excerpt",
    meta_description: "Saved meta",
    keywords: ["herald"],
    tags: ["python"],
    cover_image_url: null,
    content_type: "announcement",
    status: "draft",
    word_count: 2,
    read_minutes: 1,
    confidence: 0.9,
    generated_by_model: null,
    updated_at: "2026-07-30T10:00:00Z",
    publications: [],
    seo_issues: [],
    seo_score: 80,
    focus_keyword: "herald",
    slug: "saved-title",
    canonical_url: null,
    ...overrides,
  };
}

function draw() {
  return render(
    <MemoryRouter initialEntries={["/content/3"]}>
      <Routes>
        <Route path="/content/:contentId" element={<ContentEditor />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Put an unsaved buffer in storage, as a crashed tab would have left one. */
function storeBuffer(fields, at = Date.now()) {
  window.localStorage.setItem(
    "herald:draft:3",
    JSON.stringify({
      at,
      draft: {
        title: "Saved title",
        body_markdown: "Saved body",
        excerpt: "Saved excerpt",
        meta_description: "Saved meta",
        keywords: "herald",
        tags: "python",
        cover_image_url: "",
        ...fields,
      },
    }),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  api.getContent.mockResolvedValue(content());
  api.platforms.mockResolvedValue([]);
});

describe("recovering unsaved work", () => {
  it("offers a buffer that says something the server does not", async () => {
    storeBuffer({ body_markdown: "A morning of writing" });
    draw();
    expect(await screen.findByRole("status")).toHaveTextContent(/Unsaved edits/);
  });

  it("does not apply it on its own — the server's copy is what was committed to", async () => {
    storeBuffer({ title: "Rescued title" });
    draw();
    await screen.findByRole("status");
    expect(screen.getByLabelText(/^Title/i)).toHaveValue("Saved title");
  });

  it("restores only when asked", async () => {
    storeBuffer({ title: "Rescued title" });
    draw();
    await screen.findByRole("status");
    await userEvent.click(screen.getByRole("button", { name: "Restore them" }));

    expect(screen.getByLabelText(/^Title/i)).toHaveValue("Rescued title");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps the buffer on disk until the user resolves the offer", async () => {
    // The regression this guards: `draft` is initialised to the server's copy
    // on arrival, so an unconditional mirror writes "no difference" over the
    // buffer and deletes it. Recovery would then survive exactly one page
    // load, and merely opening the editor would be what destroyed the work.
    storeBuffer({ body_markdown: "A morning of writing" });
    draw();
    await screen.findByRole("status");

    await waitFor(() => expect(draftStore.load(3)).not.toBeNull());
    expect(draftStore.load(3).draft.body_markdown).toBe("A morning of writing");
  });

  it("drops the buffer when the offer is declined", async () => {
    storeBuffer({ body_markdown: "Not wanted" });
    draw();
    await screen.findByRole("status");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(draftStore.load(3)).toBeNull();
  });

  it("says nothing when the buffer matches what was since saved", async () => {
    // Saved from another tab: the buffer is stale, not a recovery.
    storeBuffer({});
    draw();
    await screen.findByDisplayValue("Saved title");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says nothing about a published piece, which cannot be edited anyway", async () => {
    api.getContent.mockResolvedValue(content({ status: "published" }));
    storeBuffer({ title: "Rescued title" });
    draw();
    await screen.findByDisplayValue("Saved title");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("mirroring edits", () => {
  it("writes what is typed to the buffer", async () => {
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    await userEvent.type(title, "!");

    await waitFor(() => expect(draftStore.load(3)?.draft.title).toBe("Saved title!"));
  });

  it("clears the buffer once the server has the text", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    await userEvent.type(title, "!");
    await waitFor(() => expect(draftStore.load(3)).not.toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(draftStore.load(3)).toBeNull());
  });

  it("keeps the buffer when the save fails — that is when it matters most", async () => {
    api.updateContent.mockRejectedValue(new Error("Service unavailable"));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    await userEvent.type(title, "!");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(draftStore.load(3)?.draft.title).toBe("Saved title!");
  });

  it("does not mirror a published piece", async () => {
    api.getContent.mockResolvedValue(content({ status: "published" }));
    draw();
    await screen.findByDisplayValue("Saved title");
    expect(draftStore.load(3)).toBeNull();
  });
});

describe("the save affordance", () => {
  it("is inert until something changes", async () => {
    draw();
    expect(await screen.findByRole("button", { name: "Saved" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/^Title/i), "!");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("saves on ⌘S", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    await userEvent.type(await screen.findByLabelText(/^Title/i), "!");
    await userEvent.keyboard("{Meta>}s{/Meta}");

    await waitFor(() => expect(api.updateContent).toHaveBeenCalled());
  });

  it("leaves ⌘S alone when there is nothing to save", async () => {
    draw();
    await screen.findByDisplayValue("Saved title");
    await userEvent.keyboard("{Meta>}s{/Meta}");
    expect(api.updateContent).not.toHaveBeenCalled();
  });
});
