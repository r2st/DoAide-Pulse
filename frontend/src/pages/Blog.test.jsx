import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import Blog from "./Blog";

const wrap = () =>
  render(
    <MemoryRouter>
      <Blog />
    </MemoryRouter>,
  );

describe("Blog", () => {
  it("lists all 3 static articles", () => {
    wrap();
    expect(screen.getByText(/AI-Powered Newsletters Drive 3x/)).toBeInTheDocument();
    expect(screen.getByText(/Subject Line Formulas/)).toBeInTheDocument();
    expect(screen.getByText(/Newsletter That Converts/)).toBeInTheDocument();
  });

  it("shows article descriptions", () => {
    wrap();
    expect(screen.getByText(/artificial intelligence transforms/)).toBeInTheDocument();
  });
});
