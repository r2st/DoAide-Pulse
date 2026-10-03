import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import NewsletterRoiCalculator from "./NewsletterRoiCalculator";

const wrap = () =>
  render(
    <MemoryRouter>
      <NewsletterRoiCalculator />
    </MemoryRouter>,
  );

describe("NewsletterRoiCalculator", () => {
  it("renders all input fields", () => {
    wrap();
    expect(screen.getByLabelText(/subscribers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/open rate/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/click rate/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/conversion rate/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/revenue per conversion/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/monthly cost/i)).toBeInTheDocument();
  });

  it("calculates ROI with default values", () => {
    wrap();
    const roi = screen.getByTestId("roi");
    expect(roi.textContent).toMatch(/%/);
  });

  it("shows annual projection", () => {
    wrap();
    const annual = screen.getByTestId("annual");
    expect(annual.textContent).toMatch(/\$/);
  });

  it("shows correct opens for default values", () => {
    wrap();
    const opens = screen.getByTestId("opens");
    expect(opens.textContent).toBe("1,750");
  });
});
