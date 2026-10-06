import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import SendTimeOptimizer from "./SendTimeOptimizer";

const wrap = () =>
  render(
    <MemoryRouter>
      <SendTimeOptimizer />
    </MemoryRouter>,
  );

describe("SendTimeOptimizer", () => {
  it("renders industry select", () => {
    wrap();
    expect(screen.getByLabelText(/industry/i)).toBeInTheDocument();
  });

  it("shows recommended times for Tech industry", () => {
    wrap();
    const slots = screen.getAllByTestId("time-slot");
    expect(slots).toHaveLength(2);
    expect(slots[0].textContent).toContain("Tuesday");
  });

  it("adjusts times for EST timezone", async () => {
    wrap();
    await userEvent.selectOptions(screen.getByLabelText(/audience timezone/i), "EST");
    const displays = screen.getAllByTestId("time-display");
    expect(displays.length).toBe(2);
    expect(displays[0].textContent).toMatch(/AM|PM/);
  });

  it("renders the weekly heatmap", () => {
    wrap();
    expect(screen.getByTestId("heatmap")).toBeInTheDocument();
  });

  it("wraps hours correctly for large negative offsets", async () => {
    wrap();
    await userEvent.selectOptions(screen.getByLabelText(/audience timezone/i), "PST");
    const displays = screen.getAllByTestId("time-display");
    for (const d of displays) {
      expect(d.textContent).toMatch(/^\d{1,2}:\d{2}\s[AP]M$/);
    }
  });

  it("wraps hours correctly for large positive offsets", async () => {
    wrap();
    await userEvent.selectOptions(screen.getByLabelText(/audience timezone/i), "AEST");
    const displays = screen.getAllByTestId("time-display");
    for (const d of displays) {
      expect(d.textContent).toMatch(/^\d{1,2}:\d{2}\s[AP]M$/);
    }
  });
});
