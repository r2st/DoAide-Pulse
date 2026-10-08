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

  it("renders subject lines article", () => {
    renderAt("/blog/subject-lines-40-percent-open-rates");
    expect(
      screen.getByRole("heading", { name: /40%\+ Open Rates/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/festival hook is the most powerful/)).toBeInTheDocument();
  });

  it("renders Indian creator playbook article", () => {
    renderAt("/blog/newsletter-0-to-10000-indian-creator-playbook");
    expect(
      screen.getByRole("heading", { name: /0 to 10,000 Subscribers/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Indian creator economy is booming/)).toBeInTheDocument();
  });

  it("renders deliverability guide article", () => {
    renderAt("/blog/email-deliverability-guide-avoid-spam-2026");
    expect(
      screen.getByRole("heading", { name: /Avoid the Spam Folder/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/DNS authentication is now mandatory/)).toBeInTheDocument();
  });

  it("shows 404 for unknown slug", () => {
    renderAt("/blog/nonexistent-post");
    expect(screen.getByText(/post not found/i)).toBeInTheDocument();
  });
});
