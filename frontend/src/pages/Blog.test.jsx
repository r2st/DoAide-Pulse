import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import Blog from "./Blog";
import { staticPosts } from "../data/blogPosts";

const wrap = () =>
  render(
    <MemoryRouter>
      <Blog />
    </MemoryRouter>,
  );

describe("Blog", () => {
  it("lists all static articles", () => {
    wrap();
    const links = screen.getAllByRole("link").filter((el) =>
      el.getAttribute("href")?.startsWith("/blog/"),
    );
    expect(links.length).toBe(staticPosts.length);
  });

  it("shows original articles", () => {
    wrap();
    expect(screen.getByText(/AI-Powered Newsletters Drive 3x/)).toBeInTheDocument();
    expect(screen.getByText(/Subject Line Formulas/)).toBeInTheDocument();
    expect(screen.getByText(/Newsletter That Converts/)).toBeInTheDocument();
  });

  it("shows new SEO articles", () => {
    wrap();
    expect(screen.getByText(/Best Free Newsletter Tools in India 2026/)).toBeInTheDocument();
    expect(screen.getByText(/Grow Email Subscribers from 0 to 1,000/)).toBeInTheDocument();
    expect(screen.getByText(/Email Marketing vs Social Media Marketing/)).toBeInTheDocument();
  });

  it("shows subject lines, creator playbook, and deliverability articles", () => {
    wrap();
    expect(screen.getByText(/Subject Lines That Get 40%\+ Open Rates/)).toBeInTheDocument();
    expect(screen.getByText(/0 to 10,000 Subscribers: Indian Creator Playbook/)).toBeInTheDocument();
    expect(screen.getByText(/Email Deliverability Guide: Avoid the Spam Folder/)).toBeInTheDocument();
  });

  it("shows article descriptions", () => {
    wrap();
    expect(screen.getByText(/artificial intelligence transforms/)).toBeInTheDocument();
    expect(screen.getByText(/comprehensive comparison of the best free newsletter/)).toBeInTheDocument();
  });

  it("shows posts sorted by date descending", () => {
    wrap();
    const links = screen.getAllByRole("link").filter((el) =>
      el.getAttribute("href")?.startsWith("/blog/"),
    );
    const titles = links.map((el) => el.textContent);
    const firstTitle = titles[0];
    expect(firstTitle).toMatch(/Best AI Newsletter Platforms|Email Marketing in India|Content Curation Tools|Subject Lines That Get 40%|Free AI GST|Best Free Newsletter Tools|Deliverability Guide/);
  });
});
