import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    editPassage: vi.fn(),
    listPreviewLinks: vi.fn(),
    createPreviewLink: vi.fn(),
    revokePreviewLink: vi.fn(),
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
  api.listPreviewLinks.mockResolvedValue([]);
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

describe("length figures", () => {
  it("counts the text on screen, not the last thing saved", async () => {
    // `data.word_count` stays 2 throughout: nothing is saved here. A figure
    // that only caught up on save would be wrong for as long as you were
    // writing, which is all of the time it is being looked at.
    draw();
    const body = await screen.findByLabelText(/^Body/i);
    expect(screen.getByText("2 words")).toBeInTheDocument();

    await userEvent.type(body, " and then some more");
    expect(screen.getByText("6 words")).toBeInTheDocument();
    expect(api.updateContent).not.toHaveBeenCalled();
  });

  it("reads a long piece the way the server will", async () => {
    // 990 words is exactly 4.5 minutes at 220wpm, where the server's Python
    // round() gives 4 and a naive Math.round would print 5. The byline on the
    // published post says 4, so this has to as well.
    const long = Array.from({ length: 990 }, (_, i) => `w${i}`).join(" ");
    api.getContent.mockResolvedValue(content({ body_markdown: long }));
    draw();

    expect(await screen.findByText("990 words")).toBeInTheDocument();
    expect(screen.getByText("4 min read")).toBeInTheDocument();
  });
});


describe("auto-save", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Take over the clock.
   *
   * Called *after* the editor has loaded rather than in a `beforeEach`:
   * installing fake timers before mount stalls the initial fetch, because the
   * poll that `findBy*` waits on never gets a chance to run.
   */
  function useClock() {
    vi.useFakeTimers();
  }

  /**
   * Put a value in a field.
   *
   * `fireEvent` rather than `userEvent` because these tests run on a faked
   * clock, and userEvent's own inter-keystroke delays deadlock against it.
   * What is under test here is the debounce, not the keyboard.
   */
  function typeInto(field, value) {
    fireEvent.change(field, { target: { value } });
  }

  /** Let the debounce elapse and the resulting request settle. */
  async function settle(ms = 2000) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("writes the draft once the typing stops", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    expect(api.updateContent).not.toHaveBeenCalled();

    await settle();
    expect(api.updateContent).toHaveBeenCalledWith(
      3,
      expect.objectContaining({ title: "Saved title!" }),
    );
  });

  it("restarts the countdown on every keystroke rather than saving mid-word", async () => {
    api.updateContent.mockResolvedValue(content());
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved titl");
    await settle(1500);
    typeInto(title, "Saved title!");
    await settle(1500);
    expect(api.updateContent).not.toHaveBeenCalled();

    await settle(600);
    expect(api.updateContent).toHaveBeenCalledTimes(1);
  });

  it("says when the text last reached the server", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();

    expect(screen.getByText(/Saved just now/)).toBeInTheDocument();
  });

  it("reports a failure inline rather than as a toast", async () => {
    // A toast per failed attempt is a stack of them for an offline laptop, and
    // the user never asked for the save that produced them.
    api.updateContent.mockRejectedValue(new Error("Service unavailable"));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();

    expect(screen.getByText("Auto-save failed")).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
    // Nothing is lost while it says so: the buffer still holds the text.
    expect(draftStore.load(3)?.draft.title).toBe("Saved title!");
  });

  it("does not retry a failed save until there is something new to send", async () => {
    api.updateContent.mockRejectedValue(new Error("Service unavailable"));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();
    expect(api.updateContent).toHaveBeenCalledTimes(1);

    // Ten more seconds of nothing happening is not a reason to ask again.
    await settle(10000);
    expect(api.updateContent).toHaveBeenCalledTimes(1);
  });

  it("keeps what was typed while the save was in flight", async () => {
    // The regression this guards: the response lands, `data` changes, and an
    // unconditional reset replaces every keystroke since the request went out
    // with what the server was told a moment ago.
    let release;
    api.updateContent.mockImplementation(
      () => new Promise((resolve) => { release = resolve; }),
    );
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();
    expect(api.updateContent).toHaveBeenCalledTimes(1);

    // Still typing while the request is out.
    typeInto(title, "Saved title!?");
    await act(async () => {
      release(content({ title: "Saved title!" }));
    });

    expect(title).toHaveValue("Saved title!?");
  });

  it("takes the server's copy when nothing was typed while it was in flight", async () => {
    // The other half of the merge: a normalised keyword list has to land in the
    // fields, or the editor stays permanently dirty and saves in a loop.
    api.updateContent.mockResolvedValue(
      content({ title: "Saved title!", keywords: ["herald", "seo"] }),
    );
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();

    expect(screen.getByLabelText(/^Keywords/i)).toHaveValue("herald, seo");
    await settle(10000);
    expect(api.updateContent).toHaveBeenCalledTimes(1);
  });

  it("never writes a published piece", async () => {
    api.getContent.mockResolvedValue(content({ status: "published" }));
    draw();
    await screen.findByDisplayValue("Saved title");
    useClock();

    await settle(10000);
    expect(api.updateContent).not.toHaveBeenCalled();
  });

  it("does not write underneath a recovery offer", async () => {
    // Saving here would answer the question the banner is asking, by
    // overwriting one of the two answers before the user picked either.
    storeBuffer({ title: "Rescued title" });
    draw();
    await screen.findByText(/Unsaved edits/);
    useClock();

    await settle(10000);
    expect(api.updateContent).not.toHaveBeenCalled();
  });

  it("saves what was restored, once the offer is answered", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Rescued title" }));
    storeBuffer({ title: "Rescued title" });
    draw();
    await screen.findByText(/Unsaved edits/);
    useClock();
    fireEvent.click(screen.getByRole("button", { name: "Restore them" }));

    await settle();
    expect(api.updateContent).toHaveBeenCalledWith(
      3,
      expect.objectContaining({ title: "Rescued title" }),
    );
  });
});

describe("editing one passage", () => {
  const BODY = "First paragraph here.\n\nSecond paragraph here.";
  /** Offsets of the second paragraph within BODY. */
  const SECOND = [23, 45];

  beforeEach(() => {
    api.getContent.mockResolvedValue(content({ body_markdown: BODY }));
    api.updateContent.mockResolvedValue(content({ body_markdown: BODY }));
    api.editPassage.mockResolvedValue({
      replacement: "Second, shorter.",
      operation: "shorten",
      provider: "openrouter",
      model: "some-model",
    });
  });

  /** Select a span of the body, the way dragging over it would. */
  async function select(from, to) {
    const body = await screen.findByLabelText(/^Body/i);
    body.setSelectionRange(from, to);
    fireEvent.select(body);
    return body;
  }

  it("says what it is waiting for rather than just sitting there disabled", async () => {
    draw();
    await screen.findByLabelText(/^Body/i);

    expect(screen.getByText(/Select a passage in the body/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Shorten" })).toBeDisabled();
  });

  it("stays inert for a selection too short to be a passage", async () => {
    draw();
    await select(0, 5);

    expect(screen.getByText(/at least 12 characters/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Shorten" })).toBeDisabled();
  });

  it("sends the selected passage and the operation", async () => {
    draw();
    await select(...SECOND);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    await waitFor(() =>
      expect(api.editPassage).toHaveBeenCalledWith(3, {
        selection: "Second paragraph here.",
        operation: "shorten",
        tone: null,
      }),
    );
  });

  it("splices the replacement in and leaves the rest of the body alone", async () => {
    draw();
    await select(...SECOND);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    await waitFor(() =>
      expect(screen.getByLabelText(/^Body/i)).toHaveValue(
        "First paragraph here.\n\nSecond, shorter.",
      ),
    );
  });

  it("saves before asking, because the server checks against the stored body", async () => {
    // The endpoint refuses a passage it cannot find in the saved draft — which
    // is what stops a stale editor pasting an edit over the wrong paragraph.
    // Without saving first, every edit on an unsaved draft would be a 422.
    draw();
    const body = await screen.findByLabelText(/^Body/i);
    fireEvent.change(body, { target: { value: `${BODY} And more.` } });
    body.setSelectionRange(...SECOND);
    fireEvent.select(body);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    await waitFor(() => expect(api.editPassage).toHaveBeenCalled());
    expect(api.updateContent.mock.invocationCallOrder[0]).toBeLessThan(
      api.editPassage.mock.invocationCallOrder[0],
    );
  });

  it("does not ask when saving first failed", async () => {
    api.updateContent.mockRejectedValue(new Error("Service unavailable"));
    draw();
    const body = await screen.findByLabelText(/^Body/i);
    fireEvent.change(body, { target: { value: `${BODY} And more.` } });
    body.setSelectionRange(...SECOND);
    fireEvent.select(body);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Service unavailable"));
    expect(api.editPassage).not.toHaveBeenCalled();
  });

  it("offers an undo, because a controlled textarea has no browser undo", async () => {
    draw();
    await select(...SECOND);
    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));
    await waitFor(() =>
      expect(screen.getByLabelText(/^Body/i)).toHaveValue(
        "First paragraph here.\n\nSecond, shorter.",
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "Undo edit" }));

    expect(screen.getByLabelText(/^Body/i)).toHaveValue(BODY);
  });

  it("has nothing to undo until an edit has been applied", async () => {
    draw();
    await select(...SECOND);
    expect(screen.queryByRole("button", { name: "Undo edit" })).not.toBeInTheDocument();
  });

  it("leaves the body untouched when the model could not be reached", async () => {
    api.editPassage.mockRejectedValue(
      new Error("No AI provider could be reached for this edit."),
    );
    draw();
    await select(...SECOND);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "No AI provider could be reached for this edit.",
      ),
    );
    expect(screen.getByLabelText(/^Body/i)).toHaveValue(BODY);
    expect(screen.queryByRole("button", { name: "Undo edit" })).not.toBeInTheDocument();
  });

  it("does not paste the reply over a passage that has since been deleted", async () => {
    // The author kept editing while the model was thinking, and the paragraph
    // the edit was for is gone. Splicing at the stale offsets would overwrite
    // whatever took its place.
    let release;
    api.editPassage.mockImplementation(
      () => new Promise((resolve) => { release = resolve; }),
    );
    draw();
    const body = await select(...SECOND);
    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));
    await waitFor(() => expect(api.editPassage).toHaveBeenCalled());

    fireEvent.change(body, { target: { value: "A wholly different draft." } });
    await act(async () => {
      release({ replacement: "Second, shorter.", operation: "shorten" });
    });

    expect(body).toHaveValue("A wholly different draft.");
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining("That passage has changed"),
    );
  });

  it("passes the chosen tone, and nothing at all for the project's own", async () => {
    draw();
    await select(...SECOND);

    await userEvent.selectOptions(screen.getByLabelText("Tone"), "casual");
    await userEvent.click(screen.getByRole("button", { name: "Retone" }));

    await waitFor(() =>
      expect(api.editPassage).toHaveBeenCalledWith(
        3,
        expect.objectContaining({ operation: "retone", tone: "casual" }),
      ),
    );
  });

  it("does not save underneath an unanswered recovery offer", async () => {
    // The save this would do first is the one the auto-save refuses to make:
    // it answers the banner's question by overwriting one of the two answers.
    storeBuffer({ body_markdown: "A morning of writing" });
    draw();
    await screen.findByText(/Unsaved edits/);
    const body = screen.getByLabelText(/^Body/i);
    fireEvent.change(body, { target: { value: `${BODY} And more.` } });
    body.setSelectionRange(...SECOND);
    fireEvent.select(body);

    await userEvent.click(screen.getByRole("button", { name: "Shorten" }));

    expect(api.updateContent).not.toHaveBeenCalled();
    expect(api.editPassage).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      "Restore or discard the unsaved edits above first.",
    );
  });

  it("is not offered on a published piece, which cannot be edited", async () => {
    api.getContent.mockResolvedValue(
      content({ body_markdown: BODY, status: "published" }),
    );
    draw();
    await screen.findByLabelText(/^Body/i);

    expect(screen.queryByRole("button", { name: "Shorten" })).not.toBeInTheDocument();
  });
});

describe("approving", () => {
  it("says where it is going when approving queued it", async () => {
    // On a project set to publish on its own, approving is the publish. The
    // toast has to say so — "ready to publish" points at a button the user no
    // longer needs to press.
    api.getContent.mockResolvedValue(content({ status: "review" }));
    api.approveContent.mockResolvedValue(
      content({
        status: "approved",
        publications: [
          { id: 1, platform: "devto", status: "pending" },
          { id: 2, platform: "bluesky", status: "scheduled" },
        ],
      }),
    );
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(toast.success).toHaveBeenCalledWith(
      "Approved — publishing to devto, bluesky",
    );
  });

  it("still says 'ready to publish' when nothing was queued", async () => {
    api.getContent.mockResolvedValue(content({ status: "review" }));
    api.approveContent.mockResolvedValue(
      content({ status: "approved", publications: [] }),
    );
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(toast.success).toHaveBeenCalledWith("Approved — ready to publish");
  });
});

describe("sharing a preview link", () => {
  beforeEach(() => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  it("shows nothing to revoke when there are no live links", async () => {
    api.listPreviewLinks.mockResolvedValue([]);
    draw();
    await screen.findByDisplayValue("Saved title");

    expect(
      await screen.findByText(/read-only link, no account required/),
    ).toBeInTheDocument();
  });

  it("shows the url once, right after creating a link", async () => {
    api.listPreviewLinks.mockResolvedValue([]);
    api.createPreviewLink.mockResolvedValue({
      id: 9,
      url: "https://herald.example.com/preview/abc123",
      expires_at: "2026-08-16T10:00:00Z",
      revoked_at: null,
      view_count: 0,
      last_viewed_at: null,
    });
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "New link" }));

    expect(
      await screen.findByText("https://herald.example.com/preview/abc123"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Shown once/)).toBeInTheDocument();
  });

  it("copies the link to the clipboard", async () => {
    api.listPreviewLinks.mockResolvedValue([]);
    api.createPreviewLink.mockResolvedValue({
      id: 9,
      url: "https://herald.example.com/preview/abc123",
      expires_at: "2026-08-16T10:00:00Z",
      revoked_at: null,
      view_count: 0,
      last_viewed_at: null,
    });
    draw();
    await screen.findByDisplayValue("Saved title");
    await userEvent.click(screen.getByRole("button", { name: "New link" }));
    await screen.findByText("https://herald.example.com/preview/abc123");

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "https://herald.example.com/preview/abc123",
    );
    expect(toast.success).toHaveBeenCalledWith("Link copied");
  });

  it("lists an existing link without its url", async () => {
    api.listPreviewLinks.mockResolvedValue([
      {
        id: 4,
        url: null,
        expires_at: "2026-08-16T10:00:00Z",
        revoked_at: null,
        view_count: 3,
        last_viewed_at: "2026-08-10T10:00:00Z",
      },
    ]);
    draw();

    expect(await screen.findByText(/viewed 3×/)).toBeInTheDocument();
    expect(screen.queryByText(/^https?:\/\//)).not.toBeInTheDocument();
  });

  it("revoking a link removes it from the list", async () => {
    api.listPreviewLinks.mockResolvedValueOnce([
      {
        id: 4,
        url: null,
        expires_at: "2026-08-16T10:00:00Z",
        revoked_at: null,
        view_count: 0,
        last_viewed_at: null,
      },
    ]);
    api.revokePreviewLink.mockResolvedValue(undefined);
    draw();
    await screen.findByText(/never opened/);

    api.listPreviewLinks.mockResolvedValueOnce([
      {
        id: 4,
        url: null,
        expires_at: "2026-08-16T10:00:00Z",
        revoked_at: "2026-08-09T10:00:00Z",
        view_count: 0,
        last_viewed_at: null,
      },
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));

    expect(api.revokePreviewLink).toHaveBeenCalledWith(3, 4);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument(),
    );
  });
});
