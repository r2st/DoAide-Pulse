import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentEditor from "./ContentEditor";
import { api } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { ROUTER_FUTURE } from "../lib/routerFuture";

/**
 * The editor's right-hand column, the publish dialog, and what a published
 * piece is allowed to do.
 *
 * `ContentEditor.test.jsx` is about the writing surface — the buffer, the
 * autosave, the passage edit, the approve. The 1,300-line page also carries
 * five panels down its side and a publish dialog, and those were the untested
 * half: between them they own the SEO fields, the link check's three verdicts,
 * the publications list with its retry, and every rule about which platform you
 * are allowed to send to.
 *
 * Separate file rather than a sixth describe in the existing one, because these
 * need mocks that one does not set (`retryPublication`, populated
 * `publications`, a `platforms` list) and it is already the second-longest test
 * file in the frontend.
 */

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
    retryPublication: vi.fn(),
    publishContent: vi.fn(),
  },
}));

const toast = { success: vi.fn(), error: vi.fn() };
vi.mock("../components/ui/Toast", () => ({ useToast: () => toast }));

function content(overrides = {}) {
  return {
    id: 3,
    project_id: 7,
    project_name: "Pulse",
    title: "Saved title",
    body_markdown: "Saved body",
    excerpt: "Saved excerpt",
    meta_description: "Saved meta",
    keywords: ["pulse"],
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
    focus_keyword: "pulse",
    slug: "saved-title",
    canonical_url: null,
    ...overrides,
  };
}

function publication(overrides = {}) {
  return {
    id: 11,
    platform: "devto",
    status: "published",
    external_url: "https://dev.to/h/saved-title",
    scheduled_for: null,
    error: null,
    ...overrides,
  };
}

/** A platform tile as `GET /platforms` describes one. */
function platform(name, { implemented = true, status = "connected" } = {}) {
  return {
    platform: name,
    display_name: name === "devto" ? "Dev.to" : name === "medium" ? "Medium" : name,
    implemented,
    connection: status ? { status } : null,
  };
}

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={["/content/3"]}>
      <Routes>
        <Route path="/content/:contentId" element={<ContentEditor />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Render and wait for the page past its loading state. */
async function open(overrides = {}, platforms = []) {
  api.getContent.mockResolvedValue(content(overrides));
  api.platforms.mockResolvedValue(platforms);
  draw();
  await screen.findByDisplayValue("Saved title");
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  api.getContent.mockResolvedValue(content());
  api.platforms.mockResolvedValue([]);
  api.listPreviewLinks.mockResolvedValue([]);
  // The auto-save is on a real 2-second timer in this file, and several tests
  // here type into a field — so on a machine slow enough for the typing itself
  // to outlast the debounce, the timer fires *during* the test rather than
  // being cleared by the unmount after it. An unstubbed `updateContent` then
  // resolves `undefined`, which the editor took for a saved piece and crashed
  // on: a failure with no connection to what the test was about, appearing only
  // under load. Answering the way the server does costs nothing and makes the
  // file's timing irrelevant.
  api.updateContent.mockResolvedValue(content());
});

// --------------------------------------------------------------------------- //
// The SEO panel                                                                //
// --------------------------------------------------------------------------- //

describe("the SEO panel", () => {
  it("explains what a cover image is for while there is no URL to show", async () => {
    await open({ cover_image_url: null });

    expect(screen.getByText(/Must be an absolute URL/i)).toBeInTheDocument();
    expect(document.querySelector("aside img")).toBeNull();
  });

  it("renders the image itself once a URL is typed, which is the real check", async () => {
    // A thumbnail is the same fetch the platforms will make. A text field that
    // merely accepted the string would report a 404 as fine.
    await open({ cover_image_url: "https://cdn.test/cover.png" });

    const img = document.querySelector("aside img");
    expect(img).toHaveAttribute("src", "https://cdn.test/cover.png");
    expect(screen.queryByText(/Must be an absolute URL/i)).not.toBeInTheDocument();
  });

  it("says so when the URL does not load an image", async () => {
    await open({ cover_image_url: "https://cdn.test/missing.png" });

    fireEvent.error(document.querySelector("aside img"));

    expect(await screen.findByText(/did not load an image/i)).toBeInTheDocument();
  });

  it("re-tries the thumbnail after the URL is edited rather than staying broken", async () => {
    // Without clearing the flag, one typo would leave the panel claiming every
    // subsequent URL is broken without ever having fetched it.
    const user = userEvent.setup();
    await open({ cover_image_url: "https://cdn.test/missing.png" });
    fireEvent.error(document.querySelector("aside img"));
    await screen.findByText(/did not load an image/i);

    await user.type(screen.getByLabelText("Cover image"), "x");

    expect(screen.queryByText(/did not load an image/i)).not.toBeInTheDocument();
    expect(document.querySelector("aside img")).toBeInTheDocument();
  });

  it("counts the meta description against the length Google truncates at", async () => {
    await open({ meta_description: "Twelve chars" });

    expect(screen.getByText("12/155")).toBeInTheDocument();
  });

  it("recounts as you type rather than reporting the last save", async () => {
    const user = userEvent.setup();
    await open({ meta_description: "" });

    await user.type(screen.getByLabelText("Meta description"), "abcde");

    expect(screen.getByText("5/155")).toBeInTheDocument();
  });

  it("says there are no issues rather than showing an empty list", async () => {
    await open({ seo_issues: [] });

    expect(screen.getByText("No SEO issues.")).toBeInTheDocument();
  });

  it("lists every issue the server reported", async () => {
    await open({
      seo_issues: [
        { level: "error", message: "No focus keyword." },
        { level: "warning", message: "Title is long." },
      ],
    });

    expect(screen.getByText("No focus keyword.")).toBeInTheDocument();
    expect(screen.getByText("Title is long.")).toBeInTheDocument();
    expect(screen.queryByText("No SEO issues.")).not.toBeInTheDocument();
  });

  it("distinguishes an error from a warning, which is the only reason level is sent", async () => {
    await open({
      seo_issues: [
        { level: "error", message: "No focus keyword." },
        { level: "warning", message: "Title is long." },
      ],
    });

    expect(screen.getByText("No focus keyword.").className).toMatch(/bad/);
    expect(screen.getByText("Title is long.").className).toMatch(/warn/);
  });

  it("seeds the list fields from the arrays the API sends", async () => {
    await open({ keywords: ["pulse", "ci"], tags: ["python", "devops"] });

    expect(screen.getByLabelText("Keywords")).toHaveValue("pulse, ci");
    expect(screen.getByLabelText("Platform tags")).toHaveValue("python, devops");
  });
});

// --------------------------------------------------------------------------- //
// The link check                                                               //
// --------------------------------------------------------------------------- //

describe("the link check", () => {
  const checkButton = () => screen.getByRole("button", { name: /^Check$/ });

  it("does not run on load, because a draft has links you are still typing", async () => {
    await open();

    expect(api.checkLinks).not.toHaveBeenCalled();
    expect(screen.getByText(/invent plausible documentation URLs/i)).toBeInTheDocument();
  });

  it("checks on demand and then offers to re-check", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({ checked: 1, broken_count: 0, links: [] });
    await open();

    await user.click(checkButton());

    expect(api.checkLinks).toHaveBeenCalledWith(3);
    expect(await screen.findByRole("button", { name: "Re-check" })).toBeInTheDocument();
  });

  it("says a body with no links has none, rather than showing nothing", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({ checked: 0, broken_count: 0, links: [] });
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("No links in the body.")).toBeInTheDocument();
  });

  it("reports a clean run in the singular for one link", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 1,
      broken_count: 0,
      links: [{ url: "https://a.test", status: "ok", http_status: 200 }],
    });
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("1 link, none dead.")).toBeInTheDocument();
  });

  it("pluralises a clean run of several", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 3,
      broken_count: 0,
      links: [{ url: "https://a.test", status: "ok", http_status: 200 }],
    });
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("3 links, none dead.")).toBeInTheDocument();
  });

  it("leads with the dead count when there is one", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 4,
      broken_count: 2,
      links: [{ url: "https://a.test", status: "broken", detail: "404" }],
    });
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("2 of 4 dead.")).toBeInTheDocument();
  });

  it("keeps 'unknown' separate from 'broken', which is the whole point", async () => {
    // Only a 404 or 410 is a fact. A timeout is the checker failing, not the
    // link, and colouring them the same would send people to fix working URLs.
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 2,
      broken_count: 1,
      links: [
        { url: "https://gone.test", status: "broken", detail: "404 Not Found" },
        { url: "https://slow.test", status: "unknown", detail: "Timed out" },
      ],
    });
    await open();

    await user.click(checkButton());

    const dead = (await screen.findByText("https://gone.test")).closest("li");
    const unsure = screen.getByText("https://slow.test").closest("li");
    expect(dead.className).toMatch(/bad/);
    expect(unsure.className).toMatch(/warn/);
    expect(unsure.className).not.toMatch(/bad/);
  });

  it("sorts the dead to the top and the fine ones to the bottom", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 3,
      broken_count: 1,
      links: [
        { url: "https://fine.test", status: "ok", http_status: 200 },
        { url: "https://slow.test", status: "unknown", detail: "Timed out" },
        { url: "https://gone.test", status: "broken", detail: "404 Not Found" },
      ],
    });
    await open();

    await user.click(checkButton());
    await screen.findByText("https://gone.test");

    const urls = [...document.querySelectorAll("aside li span.font-mono")]
      .map((node) => node.textContent)
      .filter((text) => text.startsWith("https://"));
    expect(urls).toEqual([
      "https://gone.test",
      "https://slow.test",
      "https://fine.test",
    ]);
  });

  it("shows the HTTP status for a link that is fine and the detail otherwise", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockResolvedValue({
      checked: 2,
      broken_count: 1,
      links: [
        { url: "https://fine.test", status: "ok", http_status: 301 },
        { url: "https://gone.test", status: "broken", detail: "404 Not Found" },
      ],
    });
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("OK (301)")).toBeInTheDocument();
    expect(screen.getByText("404 Not Found")).toBeInTheDocument();
  });

  it("says it is checking while the request is out", async () => {
    const user = userEvent.setup();
    let release;
    api.checkLinks.mockReturnValue(new Promise((resolve) => (release = resolve)));
    await open();

    await user.click(checkButton());

    expect(await screen.findByRole("button", { name: "Checking…" })).toBeDisabled();

    await act(async () => release({ checked: 0, broken_count: 0, links: [] }));
  });

  it("reports a failed check in the panel rather than losing it", async () => {
    const user = userEvent.setup();
    api.checkLinks.mockRejectedValue(new Error("Checker unavailable"));
    await open();

    await user.click(checkButton());

    expect(await screen.findByText("Checker unavailable")).toBeInTheDocument();
    // The explanation gives way to the error rather than sitting above it.
    expect(
      screen.queryByText(/invent plausible documentation URLs/i),
    ).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The publications panel                                                       //
// --------------------------------------------------------------------------- //

describe("the publications panel", () => {
  it("says nothing has been sent rather than showing an empty list", async () => {
    await open({ publications: [] });

    expect(screen.getByText("Not sent anywhere yet.")).toBeInTheDocument();
  });

  it("names the platform and links to where it went", async () => {
    await open({ publications: [publication()] });

    const link = screen.getByRole("link", { name: "https://dev.to/h/saved-title" });
    expect(link).toHaveAttribute("href", "https://dev.to/h/saved-title");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("shows the scheduled time only while it is still scheduled", async () => {
    // A row that published at noon should not still be advertising the time it
    // was going to go out.
    await open({
      publications: [
        publication({
          id: 12,
          status: "scheduled",
          external_url: null,
          scheduled_for: "2026-08-20T09:00:00Z",
        }),
      ],
    });

    expect(document.querySelector("aside")).toHaveTextContent(
      formatDateTime("2026-08-20T09:00:00Z"),
    );
  });

  it("does not show a scheduled time on a row that already went out", async () => {
    await open({
      publications: [
        publication({ status: "published", scheduled_for: "2026-08-20T09:00:00Z" }),
      ],
    });

    expect(document.querySelector("aside")).not.toHaveTextContent(
      formatDateTime("2026-08-20T09:00:00Z"),
    );
  });

  it("shows the platform's own refusal, which is what tells you what to change", async () => {
    await open({
      publications: [
        publication({
          status: "failed",
          external_url: null,
          error: "422: tag list too long",
        }),
      ],
    });

    expect(screen.getByText("422: tag list too long")).toBeInTheDocument();
  });

  it("offers Retry on a failed row and nowhere else", async () => {
    await open({
      publications: [
        publication({ id: 11, status: "published" }),
        publication({ id: 12, platform: "medium", status: "failed", error: "500" }),
      ],
    });

    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });

  it("retries the row it was clicked on, then re-reads the piece", async () => {
    const user = userEvent.setup();
    api.retryPublication.mockResolvedValue({});
    await open({
      publications: [
        publication({ id: 12, platform: "medium", status: "failed", error: "500" }),
      ],
    });
    api.getContent.mockClear();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(api.retryPublication).toHaveBeenCalledWith(3, 12));
    // Re-read, or the row keeps saying "failed" over a publication now queued.
    await waitFor(() => expect(api.getContent).toHaveBeenCalled());
    expect(toast.success).toHaveBeenCalledWith("Retrying");
  });

  it("reports a retry the server refused instead of claiming it worked", async () => {
    const user = userEvent.setup();
    api.retryPublication.mockRejectedValue(new Error("Still rate limited"));
    await open({
      publications: [
        publication({ id: 12, platform: "medium", status: "failed", error: "500" }),
      ],
    });

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Still rate limited"));
    expect(toast.success).not.toHaveBeenCalled();
  });
});

// --------------------------------------------------------------------------- //
// The publish dialog                                                           //
// --------------------------------------------------------------------------- //

describe("the publish dialog", () => {
  async function openDialog(overrides = {}, platforms = [platform("devto")]) {
    const user = userEvent.setup();
    await open(overrides, platforms);
    await user.click(screen.getByRole("button", { name: "Publish" }));
    await screen.findByRole("heading", { name: "Publish" });
    return user;
  }

  it("will not publish with nothing selected", async () => {
    await openDialog();

    expect(screen.getByRole("button", { name: "Publish now" })).toBeDisabled();
  });

  it("enables the button once a platform is ticked", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));

    expect(screen.getByRole("button", { name: "Publish now" })).toBeEnabled();
  });

  it("shows an unfinished adapter rather than hiding it, with the reason", async () => {
    // Hiding it makes the list look arbitrary — a user who expects Medium and
    // does not see it assumes Pulse is broken.
    await openDialog({}, [platform("medium", { implemented: false })]);

    expect(screen.getByText("Adapter not finished")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Medium/ })).toBeDisabled();
  });

  it("points an unconnected platform at where the credentials go", async () => {
    await openDialog({}, [platform("devto", { status: null })]);

    expect(
      screen.getByText("Not connected — add credentials in Settings"),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Dev\.to/ })).toBeDisabled();
  });

  it("refuses a platform this piece is already live on", async () => {
    await openDialog(
      { publications: [publication({ platform: "devto", status: "published" })] },
      [platform("devto")],
    );

    expect(screen.getByText("Already published here")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Dev\.to/ })).toBeDisabled();
  });

  it("still offers a platform whose last attempt failed", async () => {
    // Only a *published* publication blocks. A failed one is precisely what the
    // dialog is being opened to fix.
    await openDialog(
      { publications: [publication({ platform: "devto", status: "failed" })] },
      [platform("devto")],
    );

    expect(screen.queryByText("Already published here")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Dev\.to/ })).toBeEnabled();
  });

  it("says 'already published' ahead of the other reasons for the same tile", async () => {
    // All three can be true at once; the one that is about *this piece* is the
    // one worth reading.
    await openDialog(
      { publications: [publication({ platform: "devto", status: "published" })] },
      [platform("devto", { implemented: false, status: null })],
    );

    expect(screen.getByText("Already published here")).toBeInTheDocument();
    expect(screen.queryByText("Adapter not finished")).not.toBeInTheDocument();
  });

  it("sends the ticked platforms, publishing now when no time is given", async () => {
    const user = await openDialog({}, [platform("devto"), platform("medium")]);
    api.publishContent.mockResolvedValue({});

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("checkbox", { name: /Medium/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalled());
    expect(api.publishContent.mock.calls[0][1]).toMatchObject({
      platforms: ["devto", "medium"],
      scheduled_for: null,
      as_draft: false,
      allow_broken_links: false,
    });
  });

  it("un-ticking removes a platform rather than sending it twice", async () => {
    const user = await openDialog({}, [platform("devto"), platform("medium")]);
    api.publishContent.mockResolvedValue({});

    const devto = screen.getByRole("checkbox", { name: /Dev\.to/ });
    await user.click(devto);
    await user.click(screen.getByRole("checkbox", { name: /Medium/ }));
    await user.click(devto);
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalled());
    expect(api.publishContent.mock.calls[0][1].platforms).toEqual(["medium"]);
  });

  it("reads the time as local and sends it as an instant", async () => {
    // `datetime-local` has no offset. The browser's own is the only honest
    // reading of what the user typed into it.
    const user = await openDialog();
    api.publishContent.mockResolvedValue({});

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.type(screen.getByLabelText(/^When/), "2026-09-01T09:30");
    await user.click(screen.getByRole("button", { name: "Schedule" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalled());
    expect(api.publishContent.mock.calls[0][1].scheduled_for).toBe(
      new Date("2026-09-01T09:30").toISOString(),
    );
  });

  it("calls the button Schedule once a time is set", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    expect(screen.getByRole("button", { name: "Publish now" })).toBeInTheDocument();

    await user.type(screen.getByLabelText(/^When/), "2026-09-01T09:30");

    expect(screen.getByRole("button", { name: "Schedule" })).toBeInTheDocument();
  });

  it("passes the platform-draft choice through", async () => {
    const user = await openDialog();
    api.publishContent.mockResolvedValue({});

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("checkbox", { name: /Create as a draft/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalled());
    expect(api.publishContent.mock.calls[0][1].as_draft).toBe(true);
  });

  it("closes, re-reads and says so on success", async () => {
    const user = await openDialog();
    api.publishContent.mockResolvedValue({});
    api.getContent.mockClear();

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Publish" })).not.toBeInTheDocument(),
    );
    expect(api.getContent).toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith("Queued for publishing");
  });

  it("keeps the dialog open with the selection intact when the server refuses", async () => {
    const user = await openDialog();
    api.publishContent.mockRejectedValue(new Error("Project has no live URL"));

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Project has no live URL"),
    );
    expect(screen.getByRole("heading", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Dev\.to/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Publish now" })).toBeEnabled();
  });

  it("answers a dead-link refusal in the form rather than with a toast", async () => {
    // The override that answers it is a checkbox in this dialog. A toast would
    // name a flag with nothing to click.
    const user = await openDialog();
    api.publishContent.mockRejectedValue(
      new Error(
        "2 dead links: https://gone.test. Fix them, or set allow_broken_links.",
      ),
    );

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    expect(await screen.findByRole("checkbox", { name: /Publish anyway/ })).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("drops the server's advice about the flag it just rendered a checkbox for", async () => {
    const user = await openDialog();
    api.publishContent.mockRejectedValue(
      new Error(
        "2 dead links: https://gone.test. Fix them, or set allow_broken_links.",
      ),
    );

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    expect(
      await screen.findByText("2 dead links: https://gone.test."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/allow_broken_links/)).not.toBeInTheDocument();
  });

  it("sends the override on the second attempt", async () => {
    const user = await openDialog();
    api.publishContent.mockRejectedValue(
      new Error("2 dead links. Fix them, or set allow_broken_links."),
    );

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));
    await screen.findByRole("checkbox", { name: /Publish anyway/ });

    api.publishContent.mockResolvedValue({});
    await user.click(screen.getByRole("checkbox", { name: /Publish anyway/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalledTimes(2));
    expect(api.publishContent.mock.calls[1][1].allow_broken_links).toBe(true);
  });

  it("does not send the override until it is ticked", async () => {
    const user = await openDialog();
    api.publishContent.mockRejectedValue(
      new Error("2 dead links. Fix them, or set allow_broken_links."),
    );

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));
    await screen.findByRole("checkbox", { name: /Publish anyway/ });
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    await waitFor(() => expect(api.publishContent).toHaveBeenCalledTimes(2));
    expect(api.publishContent.mock.calls[1][1].allow_broken_links).toBe(false);
  });

  it("cancels without publishing", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Publish" })).not.toBeInTheDocument(),
    );
    expect(api.publishContent).not.toHaveBeenCalled();
  });

  it("blocks a second submit while the first is in flight", async () => {
    const user = await openDialog();
    let release;
    api.publishContent.mockReturnValue(new Promise((resolve) => (release = resolve)));

    await user.click(screen.getByRole("checkbox", { name: /Dev\.to/ }));
    await user.click(screen.getByRole("button", { name: "Publish now" }));

    expect(await screen.findByRole("button", { name: "Queueing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();

    await act(async () => release({}));
  });

  it("copes with a platforms request that has not answered yet", async () => {
    // `platforms ?? []` is the whole guard. Without it the dialog throws on
    // `.map` and takes the editor down with it.
    const user = userEvent.setup();
    api.getContent.mockResolvedValue(content());
    api.platforms.mockReturnValue(new Promise(() => {}));
    draw();
    await screen.findByDisplayValue("Saved title");

    await user.click(screen.getByRole("button", { name: "Publish" }));

    expect(await screen.findByRole("heading", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish now" })).toBeDisabled();
  });
});

// --------------------------------------------------------------------------- //
// Write and preview                                                            //
// --------------------------------------------------------------------------- //

describe("the write and preview tabs", () => {
  it("opens on the writing surface", async () => {
    await open();

    expect(screen.getByLabelText("Title")).toBeInTheDocument();
    expect(document.querySelector("article.prose-pulse")).toBeNull();
  });

  it("renders the draft as markdown under preview", async () => {
    const user = userEvent.setup();
    await open({ body_markdown: "## A heading\n\nSome prose." });

    await user.click(screen.getByRole("button", { name: "Preview" }));

    const article = document.querySelector("article.prose-pulse");
    expect(within(article).getByRole("heading", { level: 2 })).toHaveTextContent(
      "A heading",
    );
    expect(article).toHaveTextContent("Some prose.");
  });

  it("previews what is typed, not what was last saved", async () => {
    const user = userEvent.setup();
    await open({ body_markdown: "Saved body" });

    await user.clear(screen.getByLabelText(/^Body/));
    await user.type(screen.getByLabelText(/^Body/), "Unsaved body");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(document.querySelector("article.prose-pulse")).toHaveTextContent(
      "Unsaved body",
    );
  });

  it("escapes the title rather than letting it inject markup", async () => {
    // The title is interpolated into an <h1> outside the markdown renderer, so
    // it needs its own escape — this asserts that escape exists.
    const user = userEvent.setup();
    await open({ title: "Saved title" });

    await user.clear(screen.getByLabelText("Title"));
    await user.type(screen.getByLabelText("Title"), "<img onerror=x>");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    const article = document.querySelector("article.prose-pulse");
    expect(article.querySelector("img")).toBeNull();
    expect(article).toHaveTextContent("<img onerror=x>");
  });

  it("goes back to writing with the text intact", async () => {
    const user = userEvent.setup();
    await open();

    await user.type(screen.getByLabelText(/^Body/), " and more");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await user.click(screen.getByRole("button", { name: "Write" }));

    expect(screen.getByLabelText(/^Body/)).toHaveValue("Saved body and more");
  });
});

// --------------------------------------------------------------------------- //
// A published piece                                                            //
// --------------------------------------------------------------------------- //

describe("a piece that has already gone out", () => {
  it("takes away the controls that would change it", async () => {
    await open({ status: "published" });

    expect(screen.queryByRole("button", { name: /^Save/ })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Delete this piece/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  });

  it("makes the editable fields read-only rather than merely ignoring edits", async () => {
    await open({ status: "published" });

    expect(screen.getByLabelText("Title")).toBeDisabled();
    expect(screen.getByLabelText(/^Body/)).toBeDisabled();
    expect(screen.getByLabelText("Meta description")).toBeDisabled();
    expect(screen.getByLabelText("Keywords")).toBeDisabled();
    expect(screen.getByLabelText("Platform tags")).toBeDisabled();
    expect(screen.getByLabelText("Cover image")).toBeDisabled();
    expect(screen.getByLabelText("Excerpt")).toBeDisabled();
  });

  it("does not offer Approve on something already published", async () => {
    await open({ status: "published" });

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("still offers Approve on a draft and on one in review", async () => {
    await open({ status: "review" });

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("still shows where it went, which is the reason to open it at all", async () => {
    await open({
      status: "published",
      publications: [publication()],
    });

    expect(
      screen.getByRole("link", { name: "https://dev.to/h/saved-title" }),
    ).toBeInTheDocument();
  });
});
