import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import SubjectLineTester from "./SubjectLineTester";

const wrap = () =>
  render(
    <MemoryRouter>
      <SubjectLineTester />
    </MemoryRouter>,
  );

describe("SubjectLineTester", () => {
  beforeEach(() => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  it("renders the input field", () => {
    wrap();
    expect(screen.getByLabelText(/your subject line/i)).toBeInTheDocument();
  });

  it("scores a good subject line highly", async () => {
    wrap();
    await userEvent.type(
      screen.getByLabelText(/your subject line/i),
      "5 Exclusive Tips for Better Email Engagement",
    );
    const score = parseInt(screen.getByTestId("score").textContent, 10);
    expect(score).toBeGreaterThanOrEqual(70);
  });

  it("scores an ALL CAPS subject line low", async () => {
    wrap();
    await userEvent.type(
      screen.getByLabelText(/your subject line/i),
      "BUY NOW FREE DEAL LIMITED OFFER",
    );
    const score = parseInt(screen.getByTestId("score").textContent, 10);
    expect(score).toBeLessThanOrEqual(50);
  });

  it("shows tips when text is entered", async () => {
    wrap();
    await userEvent.type(
      screen.getByLabelText(/your subject line/i),
      "Hello world",
    );
    expect(screen.getByTestId("tips")).toBeInTheDocument();
  });
});
