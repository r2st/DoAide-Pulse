/**
 * The mark, and the one decision it makes: whether it is a picture or noise.
 *
 * It appears twice next to the word "Pulse" — in the sidebar and on the
 * preview page — where announcing it would make a screen reader say the name
 * twice, and once on the sign-in page where it is the only thing naming the
 * product. `title` is what tells the two apart, and it is the kind of prop
 * that gets dropped in a refactor without anything visible changing.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Logo from "./Logo";

describe("without a title", () => {
  it("is hidden from assistive technology", () => {
    const { container } = render(<Logo />);
    const svg = container.querySelector("svg");

    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).not.toHaveAttribute("role");
  });

  it("has no title element to announce", () => {
    const { container } = render(<Logo />);
    expect(container.querySelector("title")).not.toBeInTheDocument();
  });
});

describe("with a title", () => {
  it("becomes an image with that accessible name", () => {
    render(<Logo title="Pulse" />);
    expect(screen.getByRole("img", { name: "Pulse" })).toBeInTheDocument();
  });

  it("is no longer hidden", () => {
    const { container } = render(<Logo title="Pulse" />);
    expect(container.querySelector("svg")).not.toHaveAttribute("aria-hidden");
  });
});

describe("sizing", () => {
  it("takes a caller's classes", () => {
    const { container } = render(<Logo className="h-5 w-5" />);
    expect(container.querySelector("svg")).toHaveClass("h-5", "w-5");
  });

  it("has a default size, so a caller that forgets one still gets a mark", () => {
    const { container } = render(<Logo />);
    expect(container.querySelector("svg")).toHaveClass("h-8", "w-8");
  });
});
