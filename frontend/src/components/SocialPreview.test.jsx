import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import SocialPreview from "./SocialPreview";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({ api: { socialCards: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
});

// Long enough to clip on every network, so switching tabs visibly changes the
// rendered title rather than only the tab styling.
const LONG_TITLE =
  "How we cut our continuous integration pipeline from forty minutes down to " +
  "under four using aggressive caching, a better runner, and far fewer steps";

const GOOD_DESCRIPTION =
  "A description that comfortably clears the fifty-character floor and reads like a real sentence.";

function draft(overrides = {}) {
  return {
    title: "Shipping Pulse v2",
    body_markdown: "## Why\n\nA paragraph.",
    excerpt: "",
    meta_description: GOOD_DESCRIPTION,
    cover_image_url: "https://cdn.example.com/cover.png",
    ...overrides,
  };
}

it("renders a tab per network with the first one selected", () => {
  render(<SocialPreview draft={draft()} url="https://example.com/p" />);

  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((tab) => tab.textContent)).toEqual([
    "X / Twitter",
    "LinkedIn",
    "Facebook",
    "Slack",
  ]);
  expect(tabs[0]).toHaveAttribute("aria-selected", "true");
});

it("shows the title and the domain on the card", () => {
  render(<SocialPreview draft={draft()} url="https://blog.example.com/post" />);

  expect(screen.getByText("Shipping Pulse v2")).toBeInTheDocument();
  expect(screen.getByText("blog.example.com")).toBeInTheDocument();
});

it("shows a longer title on LinkedIn than on X", async () => {
  // The reason the tabs exist: the same post is cut differently per network.
  render(<SocialPreview draft={draft({ title: LONG_TITLE })} url="https://e.com/p" />);

  const onX = screen.getByText(/^How we cut/).textContent;
  await userEvent.click(screen.getByRole("tab", { name: "LinkedIn" }));
  const onLinkedIn = screen.getByText(/^How we cut/).textContent;

  expect(onLinkedIn.length).toBeGreaterThan(onX.length);
});

it("says in words when the title is cut, not only with an ellipsis", async () => {
  render(<SocialPreview draft={draft({ title: LONG_TITLE })} url="https://e.com/p" />);

  expect(screen.getByText(/title is cut here/i)).toBeInTheDocument();
});

it("says nothing about clipping when everything fits", () => {
  render(<SocialPreview draft={draft()} url="https://e.com/p" />);

  expect(screen.queryByText(/cut here/i)).not.toBeInTheDocument();
});

// The notice has three wordings and only one was ever rendered. They exist
// because the ellipsis in the card reads as a rendering artefact rather than as
// lost words — and a notice that names the title while the description is also
// going is worse than the ellipsis, because now it is specific and wrong.

it("names the description when that is the part being cut", async () => {
  // X clips the description at 125 characters and the title at 70. A short
  // title with a long description isolates the second branch.
  render(
    <SocialPreview
      draft={draft({ title: "Short", meta_description: "d".repeat(200) })}
      url="https://e.com/p"
    />,
  );

  expect(screen.getByText(/^The description is cut here\.$/)).toBeInTheDocument();
});

it("names both when both are going", async () => {
  render(
    <SocialPreview
      draft={draft({ title: LONG_TITLE, meta_description: "d".repeat(200) })}
      url="https://e.com/p"
    />,
  );

  expect(
    screen.getByText(/^Title and description are both cut here\.$/),
  ).toBeInTheDocument();
  // Not the single-field wording as well, which is what a chain of independent
  // `&&`s rather than an if/else would produce.
  expect(screen.queryByText(/^The title is cut here\.$/)).not.toBeInTheDocument();
});

it("renders the cover image at the ratio the networks crop to", () => {
  render(<SocialPreview draft={draft()} url="https://e.com/p" />);

  const image = document.querySelector("img");
  expect(image).toHaveAttribute("src", "https://cdn.example.com/cover.png");
  expect(image.className).toContain("aspect-[1.91/1]");
});

it("falls back to the text-only card when the cover URL does not load", () => {
  // A URL that is absolute and ends in .png still 404s, and the panel exists to
  // predict the unfurl — a broken image rendered as a broken image would show
  // the browser's placeholder rather than the card a crawler will actually
  // build, which is the text-only one.
  render(<SocialPreview draft={draft()} url="https://e.com/p" />);

  const image = document.querySelector("img");
  expect(image).not.toBeNull();
  act(() => {
    fireEvent.error(image);
  });

  // Swapped for the same grey-box layout a missing cover gets. The audit above
  // still reports the card as clean, which is right — the URL is well-formed,
  // and only fetching it says otherwise.
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector("svg[aria-hidden]")).not.toBeNull();
  expect(screen.getByText("Shipping Pulse v2")).toBeInTheDocument();
});

it("falls back to the small text-only card when there is no cover", () => {
  render(<SocialPreview draft={draft({ cover_image_url: "" })} url="https://e.com/p" />);

  // No image element at all — the placeholder is an inline svg, which is what
  // the networks actually draw.
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText(/no cover image/i)).toBeInTheDocument();
});

it("treats a relative cover as no cover", () => {
  // A crawler fetches from its own servers and cannot resolve it.
  render(<SocialPreview draft={draft({ cover_image_url: "/img/c.png" })} url="https://e.com/p" />);

  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText(/relative path/i)).toBeInTheDocument();
});

it("reports a clean card when nothing is wrong", () => {
  render(<SocialPreview draft={draft()} url="https://e.com/p" />);

  expect(screen.getByText(/the card is complete/i)).toBeInTheDocument();
});

it("shows the placeholder title when the piece has none", () => {
  render(<SocialPreview draft={draft({ title: "" })} url="https://e.com/p" />);

  expect(screen.getByText(/untitled/i)).toBeInTheDocument();
});

it("does not fetch the meta tags until they are asked for", () => {
  render(<SocialPreview draft={draft()} url="https://e.com/p" contentId={7} />);

  expect(api.socialCards).not.toHaveBeenCalled();
});

it("fetches and shows the meta tags on request", async () => {
  api.socialCards.mockResolvedValue({
    meta_html: '<meta property="og:title" content="Shipping Pulse v2">',
  });
  render(<SocialPreview draft={draft()} url="https://e.com/p" contentId={7} />);

  await userEvent.click(screen.getByRole("button", { name: "Show" }));

  await waitFor(() =>
    expect(screen.getByLabelText(/meta tags/i)).toHaveValue(
      '<meta property="og:title" content="Shipping Pulse v2">',
    ),
  );
  expect(api.socialCards).toHaveBeenCalledWith(7);
});

it("selects the whole tag block on focus, since it is pasted whole", async () => {
  // The textarea is read-only and its entire contents are what goes into a
  // template's <head>. Selecting on focus means the keyboard path is click,
  // ⌘C — without it, a six-row block has to be dragged over exactly.
  api.socialCards.mockResolvedValue({ meta_html: '<meta name="a" content="b">' });
  render(<SocialPreview draft={draft()} url="https://e.com/p" contentId={7} />);
  await userEvent.click(screen.getByRole("button", { name: "Show" }));
  const field = await screen.findByLabelText(/meta tags/i);

  act(() => field.focus());

  expect(field.selectionStart).toBe(0);
  expect(field.selectionEnd).toBe(field.value.length);
  expect(field.value.length).toBeGreaterThan(0);
});

it("surfaces a failure to load the tags", async () => {
  api.socialCards.mockRejectedValue(new Error("Network unreachable"));
  render(<SocialPreview draft={draft()} url="https://e.com/p" contentId={7} />);

  await userEvent.click(screen.getByRole("button", { name: "Show" }));

  expect(await screen.findByText("Network unreachable")).toBeInTheDocument();
});

it("omits the meta tags section for an unsaved piece", () => {
  // No id means nothing to fetch against.
  render(<SocialPreview draft={draft()} url="https://e.com/p" />);

  expect(screen.queryByText(/meta tags/i)).not.toBeInTheDocument();
});

it("updates when the draft changes", () => {
  const { rerender } = render(
    <SocialPreview draft={draft()} url="https://e.com/p" />,
  );
  expect(screen.getByText("Shipping Pulse v2")).toBeInTheDocument();

  rerender(<SocialPreview draft={draft({ title: "Renamed" })} url="https://e.com/p" />);

  // The whole point of computing locally: it tracks the unsaved draft.
  expect(screen.getByText("Renamed")).toBeInTheDocument();
  expect(screen.queryByText("Shipping Pulse v2")).not.toBeInTheDocument();
});

// ---- Copying the tags -----------------------------------------------------
//
// One button doing two jobs — Show, then Copy — so the copy half only exists
// once the first has landed, and nothing had ever pressed it a second time.
// The whole section is here for pasting somewhere else; a Copy that quietly
// does nothing makes the panel decorative.

/** Load the tags, then hand back the button that is now a Copy button. */
async function showTags() {
  api.socialCards.mockResolvedValue({ meta_html: '<meta property="og:title">' });
  render(<SocialPreview draft={draft()} url="https://e.com/p" contentId={7} />);
  await userEvent.click(screen.getByRole("button", { name: "Show" }));
  return await screen.findByRole("button", { name: "Copy" });
}

it("copies the loaded tags and says so, then goes back to offering it", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });

  const copy = await showTags();
  await userEvent.click(copy);

  expect(writeText).toHaveBeenCalledWith('<meta property="og:title">');
  expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

  // The confirmation is temporary: a button reading "Copied" forever is a
  // button that no longer says what pressing it would do.
  await act(async () => {
    vi.advanceTimersByTime(2000);
  });
  expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  vi.useRealTimers();
});

it("says the copy was refused rather than claiming it worked", async () => {
  // `writeText` rejects — it does not throw — on an insecure origin or under a
  // permissions policy. Uncaught, the button would flip to "Copied" with an
  // empty clipboard behind it.
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error("Denied")) },
  });

  const copy = await showTags();
  await userEvent.click(copy);

  expect(
    await screen.findByText(/select the tags and copy them manually/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Copied" })).not.toBeInTheDocument();
  // The advice is only honest because the textarea is still there.
  expect(screen.getByLabelText(/meta tags/i)).toHaveValue('<meta property="og:title">');
});
