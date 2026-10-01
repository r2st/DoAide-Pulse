/**
 * The unauthenticated preview a reviewer sees. No sign-in, no api mocks
 * beyond the one public read — everything else about this page is `useApi`'s
 * ordinary loading/error/data cycle.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PreviewPage from "./PreviewPage";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: { publicPreview: vi.fn() },
}));

function draw() {
  return render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={["/preview/abc123"]}>
      <Routes>
        <Route path="/preview/:token" element={<PreviewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function preview(overrides = {}) {
  return {
    title: "Shipping Pulse 1.0",
    body_markdown: "## It shipped\n\nPulse 1.0 is out today.",
    excerpt: "Pulse 1.0 is out.",
    cover_image_url: null,
    word_count: 6,
    read_minutes: 1,
    project_name: "Pulse",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

it("asks the public endpoint for the token in the url, not an authenticated one", async () => {
  api.publicPreview.mockResolvedValue(preview());
  draw();
  await screen.findByText("Shipping Pulse 1.0");

  expect(api.publicPreview).toHaveBeenCalledWith("abc123");
});

describe("while the link is loading", () => {
  it("stands in for the article rather than printing the word Loading", async () => {
    // A reviewer opening a shared link sees this before anything else, and a
    // bare line of text in the corner of an empty page reads as a broken
    // link. The skeleton is the same one every other page in the app shows.
    let release;
    api.publicPreview.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const { container } = draw();

    expect(container.querySelector(".animate-shimmer")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();

    release(preview());
    await screen.findByText("Shipping Pulse 1.0");
  });

  it("still announces the wait to a screen reader", async () => {
    // `Skeleton` is `aria-hidden`, so replacing visible text with it would
    // otherwise leave a non-sighted reviewer on a silent, apparently empty
    // page for the length of the request.
    let release;
    api.publicPreview.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    draw();

    expect(screen.getByRole("status")).toHaveTextContent("Loading preview…");

    release(preview());
    await screen.findByText("Shipping Pulse 1.0");
  });

  it("clears the skeleton once the draft arrives", async () => {
    api.publicPreview.mockResolvedValue(preview());
    const { container } = draw();
    await screen.findByText("Shipping Pulse 1.0");

    expect(container.querySelector(".animate-shimmer")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("clears the skeleton when the link turns out to be dead", async () => {
    api.publicPreview.mockRejectedValue(new Error("Link not found"));
    const { container } = draw();
    await screen.findByText(/isn't available anymore/);

    expect(container.querySelector(".animate-shimmer")).not.toBeInTheDocument();
  });
});

describe("a live link", () => {
  it("renders the title and body", async () => {
    api.publicPreview.mockResolvedValue(preview());
    draw();

    expect(await screen.findByText("Shipping Pulse 1.0")).toBeInTheDocument();
    expect(screen.getByText("It shipped")).toBeInTheDocument();
    expect(screen.getByText("Pulse 1.0 is out today.")).toBeInTheDocument();
  });

  it("shows the project name and reading stats", async () => {
    api.publicPreview.mockResolvedValue(preview());
    draw();

    expect(await screen.findByText(/Pulse/)).toBeInTheDocument();
    expect(screen.getByText(/6 words/)).toBeInTheDocument();
  });

  it("shows a cover image when one is set", async () => {
    api.publicPreview.mockResolvedValue(
      preview({ cover_image_url: "https://example.com/cover.png" }),
    );
    const { container } = draw();
    await screen.findByText("Shipping Pulse 1.0");

    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "https://example.com/cover.png",
    );
  });

  it("renders no image element when there is no cover", async () => {
    api.publicPreview.mockResolvedValue(preview({ cover_image_url: null }));
    const { container } = draw();
    await screen.findByText("Shipping Pulse 1.0");

    expect(container.querySelector("img")).not.toBeInTheDocument();
  });
});

describe("a dead link", () => {
  it("says the link is unavailable rather than showing a raw error", async () => {
    api.publicPreview.mockRejectedValue(new Error("Link not found"));
    draw();

    expect(
      await screen.findByText(/isn't available anymore/),
    ).toBeInTheDocument();
  });
});
