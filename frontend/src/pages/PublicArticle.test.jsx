import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import PublicArticle from "./PublicArticle";

vi.mock("../lib/api", () => ({
  api: {
    publicArticle: vi.fn(),
  },
}));

vi.mock("../lib/markdown", () => ({
  renderMarkdown: (md) => `<p>${md}</p>`,
}));

import { api } from "../lib/api";

const wrap = (slug = "test-article") =>
  render(
    <MemoryRouter initialEntries={[`/article/${slug}`]}>
      <Routes>
        <Route path="/article/:slug" element={<PublicArticle />} />
      </Routes>
    </MemoryRouter>,
  );

describe("PublicArticle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a published article", async () => {
    api.publicArticle.mockResolvedValue({
      title: "Test Article",
      slug: "test-article",
      body_markdown: "Hello world",
      excerpt: "An excerpt",
      meta_description: "A description",
      tags: ["python"],
      word_count: 100,
      read_minutes: 1,
      project_name: "My Project",
      published_at: "2026-10-01T00:00:00",
    });
    wrap();
    expect(await screen.findByText("Test Article")).toBeInTheDocument();
    expect(screen.getByText(/My Project/)).toBeTruthy();
    expect(screen.getByText("Share this article")).toBeInTheDocument();
    expect(screen.getByText("Try DoAide Pulse for free")).toBeInTheDocument();
  });

  it("shows error state for missing article", async () => {
    api.publicArticle.mockRejectedValue(new Error("Not found"));
    wrap("nonexistent");
    expect(
      await screen.findByText(/this article isn't available/i),
    ).toBeInTheDocument();
  });

  it("shows Published with DoAide Pulse branding", async () => {
    api.publicArticle.mockResolvedValue({
      title: "Branded Article",
      slug: "branded-article",
      body_markdown: "Content",
      excerpt: "",
      tags: [],
      word_count: 50,
      read_minutes: 1,
    });
    wrap("branded-article");
    expect(
      await screen.findByText("Published with DoAide Pulse"),
    ).toBeInTheDocument();
  });
});
