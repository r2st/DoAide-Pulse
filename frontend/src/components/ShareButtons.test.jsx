import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ShareButtons from "./ShareButtons";

const wrap = (ui) => render(<MemoryRouter>{ui}</MemoryRouter>);

describe("ShareButtons", () => {
  it("renders WhatsApp, Twitter and Copy link buttons", () => {
    wrap(<ShareButtons text="hello" url="https://example.com" />);
    expect(screen.getByText("WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("Twitter / X")).toBeInTheDocument();
    expect(screen.getByText("Copy link")).toBeInTheDocument();
  });

  it("WhatsApp link encodes text and url", () => {
    wrap(<ShareButtons text="Check this" url="https://pulse.doaide.com" />);
    const wa = screen.getByText("WhatsApp");
    expect(wa.getAttribute("href")).toContain("wa.me");
    expect(wa.getAttribute("href")).toContain(
      encodeURIComponent("https://pulse.doaide.com"),
    );
  });

  it("Twitter link encodes text and url", () => {
    wrap(<ShareButtons text="Check this" url="https://pulse.doaide.com" />);
    const tw = screen.getByText("Twitter / X");
    expect(tw.getAttribute("href")).toContain("twitter.com/intent/tweet");
  });

  it('Copy link shows "Copied!" after click', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    wrap(<ShareButtons text="hello" url="https://example.com" />);
    await userEvent.click(screen.getByText("Copy link"));
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
  });
});
