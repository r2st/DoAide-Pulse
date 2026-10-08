import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import TemplatesGallery from "./TemplatesGallery";

const wrap = () =>
  render(
    <MemoryRouter>
      <TemplatesGallery />
    </MemoryRouter>,
  );

describe("TemplatesGallery", () => {
  it("renders all 11 template cards", () => {
    wrap();
    expect(screen.getByText("Marketing Campaign")).toBeInTheDocument();
    expect(screen.getByText("Product Update")).toBeInTheDocument();
    expect(screen.getByText("Weekly Digest")).toBeInTheDocument();
    expect(screen.getByText("Educational")).toBeInTheDocument();
    expect(screen.getByText("Welcome Email Series")).toBeInTheDocument();
    expect(screen.getByText("Product Launch")).toBeInTheDocument();
    expect(screen.getByText("Weekly Roundup")).toBeInTheDocument();
    expect(screen.getByText("Event Invitation")).toBeInTheDocument();
    expect(screen.getByText("Feedback Request")).toBeInTheDocument();
    const cards = screen.getAllByText("Use Template");
    expect(cards).toHaveLength(11);
  });

  it("shows category badges for new templates", () => {
    wrap();
    expect(screen.getByText("Onboarding")).toBeInTheDocument();
    expect(screen.getByText("Launch")).toBeInTheDocument();
    expect(screen.getByText("Digest")).toBeInTheDocument();
    expect(screen.getByText("Events")).toBeInTheDocument();
    expect(screen.getByText("Survey")).toBeInTheDocument();
  });

  it("expands and collapses a template preview", async () => {
    const user = userEvent.setup();
    wrap();
    const buttons = screen.getAllByText("Preview");
    await user.click(buttons[0]);
    expect(screen.getByText("Close Preview")).toBeInTheDocument();
    await user.click(screen.getByText("Close Preview"));
    expect(screen.queryByText("Close Preview")).not.toBeInTheDocument();
  });

  it("shows FAQs", () => {
    wrap();
    expect(screen.getByText("Are these newsletter templates free to use?")).toBeInTheDocument();
  });

  it("shows descriptions for new templates", () => {
    wrap();
    expect(screen.getByText(/warm welcome sequence for new subscribers/)).toBeInTheDocument();
    expect(screen.getByText(/High-impact product launch announcement/)).toBeInTheDocument();
    expect(screen.getByText(/Structured weekly digest with top stories/)).toBeInTheDocument();
    expect(screen.getByText(/Clean event invitation with date/)).toBeInTheDocument();
    expect(screen.getByText(/Friendly feedback and survey request/)).toBeInTheDocument();
  });
});
