import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import ContentIdeaGenerator from "./ContentIdeaGenerator";

vi.mock("../../lib/api", () => ({
  api: {
    generateContentIdeas: vi.fn(),
  },
}));

import { api } from "../../lib/api";

const wrap = () =>
  render(
    <MemoryRouter>
      <ContentIdeaGenerator />
    </MemoryRouter>,
  );

describe("ContentIdeaGenerator", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the input field and generate button", () => {
    wrap();
    expect(screen.getByLabelText(/your niche or topic/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate ideas/i })).toBeInTheDocument();
  });

  it("renders example niche buttons", () => {
    wrap();
    expect(screen.getByText("SaaS marketing")).toBeInTheDocument();
    expect(screen.getByText("AI tools for developers")).toBeInTheDocument();
  });

  it("clicking an example niche fills the input", async () => {
    wrap();
    await userEvent.click(screen.getByText("SaaS marketing"));
    expect(screen.getByLabelText(/your niche or topic/i).value).toBe("SaaS marketing");
  });

  it("generates and displays ideas", async () => {
    api.generateContentIdeas.mockResolvedValue({
      niche: "AI tools",
      ideas: [
        { title: "Idea One", hook: "Hook one", content_type: "tutorial" },
        { title: "Idea Two", hook: "Hook two", content_type: "how_to" },
      ],
    });
    wrap();
    await userEvent.type(screen.getByLabelText(/your niche or topic/i), "AI tools");
    await userEvent.click(screen.getByRole("button", { name: /generate ideas/i }));
    expect(await screen.findByText("Idea One")).toBeInTheDocument();
    expect(screen.getByText("Idea Two")).toBeInTheDocument();
    expect(screen.getByText("Hook one")).toBeInTheDocument();
  });

  it("shows error on API failure", async () => {
    api.generateContentIdeas.mockRejectedValue(new Error("Service unavailable"));
    wrap();
    await userEvent.type(screen.getByLabelText(/your niche or topic/i), "cooking");
    await userEvent.click(screen.getByRole("button", { name: /generate ideas/i }));
    expect(await screen.findByText(/service unavailable/i)).toBeInTheDocument();
  });

  it("disables button when input is empty", () => {
    wrap();
    const btn = screen.getByRole("button", { name: /generate ideas/i });
    expect(btn).toBeDisabled();
  });

  it("shows DoAide Pulse CTA", () => {
    wrap();
    expect(screen.getByText("Try DoAide Pulse for free")).toBeInTheDocument();
  });
});
