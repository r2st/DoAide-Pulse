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

vi.mock("../lib/api", () => ({
  api: { publicPreview: vi.fn() },
}));

function draw() {
  return render(
    <MemoryRouter initialEntries={["/preview/abc123"]}>
      <Routes>
        <Route path="/preview/:token" element={<PreviewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function preview(overrides = {}) {
  return {
    title: "Shipping Herald 1.0",
    body_markdown: "## It shipped\n\nHerald 1.0 is out today.",
    excerpt: "Herald 1.0 is out.",
    cover_image_url: null,
    word_count: 6,
    read_minutes: 1,
    project_name: "Herald",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

it("asks the public endpoint for the token in the url, not an authenticated one", async () => {
  api.publicPreview.mockResolvedValue(preview());
  draw();
  await screen.findByText("Shipping Herald 1.0");

  expect(api.publicPreview).toHaveBeenCalledWith("abc123");
});

describe("a live link", () => {
  it("renders the title and body", async () => {
    api.publicPreview.mockResolvedValue(preview());
    draw();

    expect(await screen.findByText("Shipping Herald 1.0")).toBeInTheDocument();
    expect(screen.getByText("It shipped")).toBeInTheDocument();
    expect(screen.getByText("Herald 1.0 is out today.")).toBeInTheDocument();
  });

  it("shows the project name and reading stats", async () => {
    api.publicPreview.mockResolvedValue(preview());
    draw();

    expect(await screen.findByText(/Herald/)).toBeInTheDocument();
    expect(screen.getByText(/6 words/)).toBeInTheDocument();
  });

  it("shows a cover image when one is set", async () => {
    api.publicPreview.mockResolvedValue(
      preview({ cover_image_url: "https://example.com/cover.png" }),
    );
    const { container } = draw();
    await screen.findByText("Shipping Herald 1.0");

    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "https://example.com/cover.png",
    );
  });

  it("renders no image element when there is no cover", async () => {
    api.publicPreview.mockResolvedValue(preview({ cover_image_url: null }));
    const { container } = draw();
    await screen.findByText("Shipping Herald 1.0");

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
