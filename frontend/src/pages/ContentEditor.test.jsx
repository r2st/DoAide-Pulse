import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ContentEditor from "./ContentEditor";
import { api } from "../lib/api";
import * as draftStore from "../lib/draftStore";
import { ROUTER_FUTURE } from "../lib/routerFuture";

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
    // Every save carries this back as `If-Match`, so the fixture has to have
    // one or the assertions below would pin `undefined` as the version.
    version: 4,
    ...overrides,
  };
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={["/content/3"]}>
      <Routes>
        <Route path="/content/:contentId" element={<ContentEditor />} />
        {/* The editor navigates here after a delete, and links here from its
            header. Without the destination mounted, both leave the router
            warning about an unmatched location — which the console guard in
            the test setup turns into a failure. */}
        <Route path="/content" element={<p>All content</p>} />
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

  // The offer is made once per piece, on arrival. Everything below is the same
  // bug from a different angle: the lookup used to re-run on every change of
  // `data`, and a background refresh mid-edit found the author's own live
  // typing in the buffer and offered it back to them.
  //
  // These cases all need `mockImplementation` rather than
  // `mockResolvedValue`. One resolved value is one object for every call, and
  // React bails out of a `setData` whose identity has not changed — so a
  // reload in a test never actually changed `data`, and the whole class was
  // invisible to the suite.
  describe("after a background refresh", () => {
    beforeEach(() => {
      api.getContent.mockImplementation(() => Promise.resolve(content()));
      api.approveContent.mockResolvedValue(content({ status: "approved" }));
    });

    /** Type, then make the editor refetch the piece behind the author. */
    async function typeThenRefresh() {
      const title = await screen.findByLabelText(/^Title/i);
      await userEvent.type(title, "!");
      await waitFor(() => expect(draftStore.load(3)?.draft.title).toBe("Saved title!"));

      await userEvent.click(screen.getByRole("button", { name: "Approve" }));
      await waitFor(() => expect(api.getContent).toHaveBeenCalledTimes(2));
      return title;
    }

    it("does not offer to recover the text being typed right now", async () => {
      draw();
      await typeThenRefresh();

      expect(screen.queryByText(/Unsaved edits/)).not.toBeInTheDocument();
    });

    it("keeps mirroring what is typed after it", async () => {
      // An unanswered offer suspends the mirror, so one raised about live text
      // stops the buffer following the writing it is supposedly protecting.
      draw();
      const title = await typeThenRefresh();
      await userEvent.type(title, "?");

      await waitFor(() =>
        expect(draftStore.load(3)?.draft.title).toBe("Saved title!?"),
      );
    });

    it("keeps auto-saving, which an unanswered offer would have stopped", async () => {
      // The half that made this more than a baffling banner. An offer on
      // screen also disables the auto-save — deliberately, so nothing answers
      // the question for the user. Raised about live text, that turns the
      // auto-save off underneath someone who is still typing and has not been
      // told they were asked anything.
      //
      // The clock is faked before the edit, because `useFakeTimers` does not
      // adopt a timer that is already running — which means `fireEvent` for
      // the rest, as everywhere else in this file that drives the debounce.
      // And it is advanced in two steps: the refresh has to land *inside* the
      // debounce window, or the save the assertion is about already went out
      // before there was anything to stop it.
      api.updateContent.mockImplementation(() =>
        Promise.resolve(content({ title: "Saved title!" })),
      );
      draw();
      const title = await screen.findByLabelText(/^Title/i);

      vi.useFakeTimers();
      try {
        fireEvent.change(title, { target: { value: "Saved title!" } });
        fireEvent.click(screen.getByRole("button", { name: "Approve" }));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
        expect(api.getContent).toHaveBeenCalledTimes(2);
        expect(api.updateContent).not.toHaveBeenCalled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000);
        });
        expect(api.updateContent).toHaveBeenCalledWith(
          3,
          expect.objectContaining({ title: "Saved title!" }),
          4,
        );
      } finally {
        vi.useRealTimers();
      }
    });

    it("still offers a buffer left by a crashed tab on first arrival", async () => {
      // The guard is "once per piece", not "never" — the feature still works.
      storeBuffer({ title: "Rescued title" });
      draw();

      expect(await screen.findByText(/Unsaved edits/)).toBeInTheDocument();
    });
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
      4,
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

  it("treats a 2xx with no piece in it as a save that did not land", async () => {
    // `lib/api` answers `null` for a body it cannot parse — a gateway timeout
    // page, a proxy's error HTML, anything that is not Herald talking. That is
    // the right reading there, and this is what happened to it here: `null`
    // went into state, the next render read `draftFrom(null).title`, and the
    // editor disappeared into its error boundary — taking the author's unsaved
    // text off the screen at the moment the save had failed to store it.
    api.updateContent.mockResolvedValue(null);
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    useClock();

    typeInto(title, "Saved title!");
    await settle();

    // Still the editor, still holding the text, saying what happened.
    expect(screen.getByLabelText(/^Title/i)).toHaveValue("Saved title!");
    expect(screen.getByText("Auto-save failed")).toBeInTheDocument();
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
      4,
    );
  });
});

describe("two people editing the same piece", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function typeInto(field, value) {
    fireEvent.change(field, { target: { value } });
  }

  async function settle(ms = 2000) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("saves against the version the fields were edited from", async () => {
    // Without this the API cannot tell an edit of the current text from an edit
    // of text somebody has since replaced, and writes both.
    api.getContent.mockResolvedValue(content({ version: 9 }));
    api.updateContent.mockResolvedValue(content({ version: 10 }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    vi.useFakeTimers();

    typeInto(title, "Mine");
    await settle();

    expect(api.updateContent).toHaveBeenCalledWith(3, expect.anything(), 9);
  });

  it("moves to the version its own save produced, rather than repeating one", async () => {
    // A precondition that stayed at the loaded version would be stale the
    // moment this editor saved once, and every save after the first would be
    // refused — the guard turned on its own user.
    api.getContent.mockResolvedValue(content({ version: 9 }));
    api.updateContent.mockResolvedValue(content({ version: 10 }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    vi.useFakeTimers();

    typeInto(title, "Mine");
    await settle();
    typeInto(title, "Mine, revised");
    await settle();

    expect(api.updateContent).toHaveBeenLastCalledWith(3, expect.anything(), 10);
  });

  it("keeps the text on screen when the server refuses a stale save", async () => {
    // The 412 is the one failure where losing the buffer would be worst: the
    // author's paragraph is the only copy of itself, and reapplying it after a
    // reload is the whole remedy the API is recommending.
    api.updateContent.mockRejectedValue(
      new Error("Somebody else has edited this piece since you loaded it"),
    );
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    vi.useFakeTimers();

    typeInto(title, "My paragraph");
    await settle();

    expect(screen.getByLabelText(/^Title/i)).toHaveValue("My paragraph");
    // The server's own words are on the status line's tooltip — "reload and
    // reapply" is very different advice from "you are offline", and the
    // difference is the only thing that tells the author what to do next.
    expect(screen.getByText("Auto-save failed")).toHaveAttribute(
      "title",
      expect.stringContaining("Somebody else has edited this piece"),
    );
    expect(draftStore.load(3)?.draft.title).toBe("My paragraph");
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

  it("does not claim anything was queued when the response omits publications", async () => {
    // `approveContent` is read for its `publications` list, and a 204-shaped or
    // trimmed response has none. Reading `.map` off that would throw inside the
    // success path and toast the TypeError as if approving had failed.
    api.getContent.mockResolvedValue(content({ status: "review" }));
    api.approveContent.mockResolvedValue(undefined);
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(toast.success).toHaveBeenCalledWith("Approved — ready to publish");
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("toasts a refused approval and leaves the piece in review", async () => {
    api.getContent.mockResolvedValue(content({ status: "review" }));
    api.approveContent.mockRejectedValue(new Error("No platform is connected"));
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("No platform is connected"),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});

describe("deleting a piece", () => {
  let confirmSpy;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it("asks for confirmation naming the piece, then leaves for the list", async () => {
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteContent.mockResolvedValue(undefined);
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: /Delete this piece/ }));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Saved title"));
    expect(api.deleteContent).toHaveBeenCalledWith(3);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Deleted"));
    // Navigating away is the only thing that makes the editor's own route
    // survivable — staying put would leave the form editing a 404.
    expect(await screen.findByText("All content")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Saved title")).not.toBeInTheDocument();
  });

  it("does nothing when the confirmation is declined", async () => {
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: /Delete this piece/ }));

    expect(api.deleteContent).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("Saved title")).toBeInTheDocument();
  });

  it("toasts a refused delete and stays on the piece", async () => {
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteContent.mockRejectedValue(new Error("Already published"));
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: /Delete this piece/ }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Already published"));
    expect(screen.getByDisplayValue("Saved title")).toBeInTheDocument();
  });
});

describe("the header line", () => {
  it("shows a retryable error instead of the form when the piece will not load", async () => {
    api.getContent.mockRejectedValue(new Error("Service Unavailable"));
    draw();

    expect(await screen.findByText("Service Unavailable")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Saved title")).not.toBeInTheDocument();

    api.getContent.mockResolvedValue(content());
    await userEvent.click(screen.getByRole("button", { name: /Retry|Try again/i }));

    expect(await screen.findByDisplayValue("Saved title")).toBeInTheDocument();
  });

  it("keeps the editor and the unsaved text when a background refresh fails", async () => {
    // Approve reloads the piece, and so do a publication retry and the publish
    // dialog — refreshes the author never asked for. Rendering their failure in
    // place of the editor unmounted a textarea that may hold minutes of unsaved
    // writing, which is the same thing the null-response guard on `persist`
    // exists to prevent, arriving by the other door.
    api.approveContent.mockResolvedValue({ publications: [] });
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    await userEvent.type(title, "!");

    api.getContent.mockRejectedValue(new Error("Service Unavailable"));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Service Unavailable")).toBeInTheDocument();
    // Still the editor, still holding what was typed.
    expect(screen.getByLabelText(/^Title/i)).toHaveValue("Saved title!");
  });

  it("drops that banner once the server answers again", async () => {
    // A save is a round trip that succeeded, so the last refresh's complaint is
    // no longer true — and a stale "Service Unavailable" over an editor that has
    // demonstrably just reached the server is worse than no banner at all.
    api.approveContent.mockResolvedValue({ publications: [] });
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    const title = await screen.findByLabelText(/^Title/i);
    await userEvent.type(title, "!");

    api.getContent.mockRejectedValue(new Error("Service Unavailable"));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await screen.findByText("Service Unavailable");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(screen.queryByText("Service Unavailable")).not.toBeInTheDocument(),
    );
  });

  it("names the model that wrote it, and says nothing when a person did", async () => {
    // Herald's own output and something typed by hand read identically once
    // saved; the byline is the only thing that distinguishes them.
    api.getContent.mockResolvedValue(
      content({ generated_by_model: "claude-opus-5" }),
    );
    const { unmount } = draw();

    expect(await screen.findByTitle("Which model wrote it")).toHaveTextContent(
      "claude-opus-5",
    );
    unmount();

    api.getContent.mockResolvedValue(content({ generated_by_model: null }));
    draw();
    await screen.findByDisplayValue("Saved title");

    expect(screen.queryByTitle("Which model wrote it")).not.toBeInTheDocument();
  });
});

describe("leaving with unsaved work", () => {
  // localStorage covers a crash; it cannot cover a reload, because the tree is
  // gone before anything in it can offer the buffer back. The browser's own
  // prompt is the only thing left, and it only appears if the handler both
  // preventDefaults and sets returnValue.
  /**
   * `Event.returnValue` is a legacy accessor on the prototype that mirrors
   * `defaultPrevented`, so reading it back would only re-assert preventDefault.
   * An own data property shadows it, which is what lets the assignment itself
   * be observed — and it is the assignment, not preventDefault, that some
   * browsers still require before they will show the prompt.
   */
  function beforeUnload() {
    const event = new Event("beforeunload", { cancelable: true });
    Object.defineProperty(event, "returnValue", {
      value: undefined,
      writable: true,
    });
    window.dispatchEvent(event);
    return event;
  }

  it("asks the browser to confirm a reload once the draft is dirty", async () => {
    draw();
    const title = await screen.findByDisplayValue("Saved title");

    await userEvent.type(title, "!");
    const event = beforeUnload();

    expect(event.defaultPrevented).toBe(true);
    expect(event.returnValue).toBe("");
  });

  it("does not interrupt a reload when nothing has been edited", async () => {
    draw();
    await screen.findByDisplayValue("Saved title");

    const event = beforeUnload();

    expect(event.defaultPrevented).toBe(false);
    expect(event.returnValue).toBeUndefined();
  });

  it("stops asking once the edit is saved", async () => {
    api.updateContent.mockResolvedValue(content({ title: "Saved title!" }));
    draw();
    const title = await screen.findByDisplayValue("Saved title");
    await userEvent.type(title, "!");

    await userEvent.click(await screen.findByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: "Saved" });

    const event = beforeUnload();

    expect(event.defaultPrevented).toBe(false);
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
      await screen.findByText(/Read-only link for external reviewers/),
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

  it("toasts a failed creation instead of leaving the button spinning", async () => {
    api.listPreviewLinks.mockResolvedValue([]);
    api.createPreviewLink.mockRejectedValue(new Error("Preview links are disabled"));
    draw();
    await screen.findByDisplayValue("Saved title");

    await userEvent.click(screen.getByRole("button", { name: "New link" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Preview links are disabled"),
    );
    expect(screen.getByRole("button", { name: "New link" })).toBeEnabled();
  });

  it("tells the user to copy by hand when the clipboard is refused", async () => {
    // Clipboard writes need a permission and a secure context; over plain http,
    // or with the permission denied, this rejects and the URL is still on
    // screen — so the advice is to select it, not to try again.
    api.listPreviewLinks.mockResolvedValue([]);
    api.createPreviewLink.mockResolvedValue({
      id: 9,
      url: "https://herald.example.com/preview/abc123",
      expires_at: "2026-08-16T10:00:00Z",
      revoked_at: null,
      view_count: 0,
      last_viewed_at: null,
    });
    navigator.clipboard.writeText.mockRejectedValue(new Error("NotAllowedError"));
    draw();
    await screen.findByDisplayValue("Saved title");
    await userEvent.click(screen.getByRole("button", { name: "New link" }));
    await screen.findByText("https://herald.example.com/preview/abc123");

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not copy — select the link and copy it manually.",
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(
      screen.getByText("https://herald.example.com/preview/abc123"),
    ).toBeInTheDocument();
  });

  it("toasts a failed revoke and keeps the link listed", async () => {
    api.listPreviewLinks.mockResolvedValue([
      {
        id: 4,
        url: null,
        expires_at: "2026-08-16T10:00:00Z",
        revoked_at: null,
        view_count: 0,
        last_viewed_at: null,
      },
    ]);
    api.revokePreviewLink.mockRejectedValue(new Error("Link already expired"));
    draw();
    await screen.findByText(/never opened/);

    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Link already expired"),
    );
    expect(screen.getByRole("button", { name: "Revoke" })).toBeEnabled();
  });

  it("can revoke the link it just created, and takes the callout down with it", async () => {
    // The new link is deliberately kept out of the list below, so before this
    // there was no revoke for it anywhere: a URL pasted into the wrong chat
    // could not be taken back until the page was reloaded, which is the one
    // moment the user most wants it gone.
    api.listPreviewLinks.mockResolvedValue([]);
    api.createPreviewLink.mockResolvedValue({
      id: 9,
      url: "https://herald.example.com/preview/abc123",
      expires_at: "2026-08-16T10:00:00Z",
      revoked_at: null,
      view_count: 0,
      last_viewed_at: null,
    });
    api.revokePreviewLink.mockResolvedValue(undefined);
    draw();
    await screen.findByDisplayValue("Saved title");
    await userEvent.click(screen.getByRole("button", { name: "New link" }));
    await screen.findByText("https://herald.example.com/preview/abc123");

    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));

    expect(api.revokePreviewLink).toHaveBeenCalledWith(3, 9);
    await waitFor(() =>
      expect(
        screen.queryByText("https://herald.example.com/preview/abc123"),
      ).not.toBeInTheDocument(),
    );
  });

  it("does not list the just-created link twice when the reload returns it", async () => {
    // The callout and the list are fed from different places — one from the
    // create response, one from the reload that follows it — and both describe
    // the same link for as long as it is the newest one.
    api.listPreviewLinks.mockResolvedValueOnce([]).mockResolvedValue([
      {
        id: 9,
        url: null,
        expires_at: "2026-08-16T10:00:00Z",
        revoked_at: null,
        view_count: 0,
        last_viewed_at: null,
      },
    ]);
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

    await waitFor(() => expect(api.listPreviewLinks).toHaveBeenCalledTimes(2));
    // One Revoke — the callout's. The list entry for the same link is filtered
    // out, so the reload does not grow a second row saying the same thing.
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(1);
    expect(screen.queryByText(/never opened/)).not.toBeInTheDocument();
  });
});
