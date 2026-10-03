import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import BlogPost from "./BlogPost";

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/blog/:slug" element={<BlogPost />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("BlogPost", () => {
  it("renders article content by slug", () => {
    renderAt("/blog/ai-newsletters-3x-engagement");
    expect(
      screen.getByRole("heading", { name: /AI-Powered Newsletters/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/newsletter landscape has shifted/)).toBeInTheDocument();
  });

  it("shows 404 for unknown slug", () => {
    renderAt("/blog/nonexistent-post");
    expect(screen.getByText(/post not found/i)).toBeInTheDocument();
  });
});
