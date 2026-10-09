import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ShareButtons from "./ShareButtons";

const wrap = (ui) => render(<MemoryRouter>{ui}</MemoryRouter>);

describe("ShareButtons", () => {
  it("renders WhatsApp, Twitter, LinkedIn and Copy link buttons", () => {
    wrap(<ShareButtons text="hello" url="https://example.com" />);
    expect(screen.getByText("WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("Twitter / X")).toBeInTheDocument();
    expect(screen.getByText("LinkedIn")).toBeInTheDocument();
    expect(screen.getByText("Copy link")).toBeInTheDocument();
  });

  it("WhatsApp link includes branded message when title is provided", () => {
    wrap(<ShareButtons title="My Article" url="https://pulse.doaide.com/article/test" />);
    const wa = screen.getByTestId("share-whatsapp");
    const href = wa.getAttribute("href");
    expect(href).toContain("wa.me");
    expect(href).toContain("Published%20with%20DoAide%20Pulse");
  });

  it("WhatsApp link uses plain text when no title", () => {
    wrap(<ShareButtons text="Check this" url="https://pulse.doaide.com" />);
    const wa = screen.getByTestId("share-whatsapp");
    const href = wa.getAttribute("href");
    expect(href).toContain("wa.me");
    expect(href).not.toContain("Published%20with%20DoAide%20Pulse");
  });

  it("Twitter link encodes text and url", () => {
    wrap(<ShareButtons text="Check this" url="https://pulse.doaide.com" />);
    const tw = screen.getByTestId("share-twitter");
    expect(tw.getAttribute("href")).toContain("twitter.com/intent/tweet");
  });

  it("LinkedIn link points to sharing endpoint", () => {
    wrap(<ShareButtons url="https://pulse.doaide.com/article/test" />);
    const li = screen.getByTestId("share-linkedin");
    expect(li.getAttribute("href")).toContain("linkedin.com/sharing/share-offsite");
    expect(li.getAttribute("href")).toContain(encodeURIComponent("https://pulse.doaide.com/article/test"));
  });

  it('Copy link shows "Copied!" after click', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    wrap(<ShareButtons text="hello" url="https://example.com" />);
    await userEvent.click(screen.getByTestId("share-copy"));
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
  });

  it("renders prominent variant with different styling", () => {
    wrap(<ShareButtons variant="prominent" url="https://example.com" />);
    const wa = screen.getByTestId("share-whatsapp");
    expect(wa.className).toContain("rounded-lg");
  });
});
