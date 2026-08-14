/**
 * The app's only chrome: wordmark, destinations, account, and the two badges.
 *
 * Untested until now, which is how it kept a `<nav>` with no name, a mobile
 * toggle that controlled nothing it named, and no way at all past nine
 * destinations for someone arriving on a new page with a keyboard.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Shell from "./Shell";
import { api } from "../lib/api";
import { ROUTER_FUTURE } from "../lib/routerFuture";

vi.mock("../lib/api", () => ({
  api: { reviewQueue: vi.fn(), publicationQueue: vi.fn() },
}));

const logout = vi.fn();
vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({ user: { email: "writer@example.com" }, logout }),
}));

/**
 * Where the router currently is.
 *
 * The chrome's only two navigations away from the current page are the pair of
 * Sign out buttons, and both do it imperatively rather than with a `<Link>` —
 * so there is no `href` to read, and the URL is the only evidence the redirect
 * happened at all.
 */
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}

/** Mount the chrome at `path` and let the two queue counts land. */
async function draw(path = "/") {
  const shell = (
    <Shell>
      <h1>The page</h1>
    </Shell>
  );
  const result = render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={[path]}>
      <Where />
      <Routes>
        <Route path="*" element={shell} />
      </Routes>
    </MemoryRouter>,
  );
  await act(async () => {});
  return result;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.reviewQueue.mockResolvedValue([]);
  api.publicationQueue.mockResolvedValue([]);
});

describe("getting past the navigation", () => {
  it("offers a skip link before anything else in the tab order", async () => {
    await draw();

    await userEvent.tab();

    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main");
  });

  it("points that link at a main element focus can actually land on", async () => {
    await draw();

    const main = document.getElementById("main");
    expect(main?.tagName).toBe("MAIN");
    // Without this the browser scrolls but leaves focus at the top of the page,
    // so the next Tab walks back into the nav the link was for skipping.
    expect(main).toHaveAttribute("tabindex", "-1");
  });

  it("hides the link until it is focused", async () => {
    await draw();

    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveClass(
      "sr-only",
      "focus:not-sr-only",
    );
  });
});

describe("the destinations", () => {
  it("names the nav, because a page has more than one of them", async () => {
    await draw();

    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("carries every destination, in the order the work happens", async () => {
    await draw();

    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Dashboard",
      "Projects",
      "Triggers",
      "Templates",
      "Content",
      "Calendar",
      "Publish",
      "Analytics",
      "Settings",
    ]);
  });

  it("marks the destination the user is on", async () => {
    await draw("/analytics");

    expect(screen.getByRole("link", { name: "Analytics" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});

describe("the badges", () => {
  it("says what the number on Content means", async () => {
    api.reviewQueue.mockResolvedValue([{ id: 1 }, { id: 2 }]);

    await draw();

    expect(screen.getByLabelText("2 awaiting review")).toHaveTextContent("2");
  });

  it("counts only the failed publications on Publish", async () => {
    api.publicationQueue.mockResolvedValue([
      { id: 1, status: "failed" },
      { id: 2, status: "queued" },
      { id: 3, status: "failed" },
    ]);

    await draw();

    expect(screen.getByLabelText("2 failed publications")).toBeInTheDocument();
  });

  it("caps the digits at 9+ so a long queue cannot widen the tab", async () => {
    api.reviewQueue.mockResolvedValue(Array.from({ length: 14 }, (_, i) => ({ id: i })));

    await draw();

    // The label keeps the real number; only the glyph is abbreviated.
    expect(screen.getByLabelText("14 awaiting review")).toHaveTextContent("9+");
  });

  it("shows nothing at zero — a badge reading 0 is a badge to ignore", async () => {
    await draw();

    expect(screen.queryByLabelText(/awaiting review/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/failed publications/)).not.toBeInTheDocument();
  });

  it("keeps the chrome up when both queue requests fail", async () => {
    api.reviewQueue.mockRejectedValue(new Error("500"));
    api.publicationQueue.mockRejectedValue(new Error("500"));

    await draw();

    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/awaiting review/)).not.toBeInTheDocument();
  });

  it("re-counts on navigation, so approving a draft updates the tab on the way out", async () => {
    await draw();
    expect(api.reviewQueue).toHaveBeenCalledTimes(1);

    api.reviewQueue.mockResolvedValue([{ id: 1 }]);
    await userEvent.click(screen.getByRole("link", { name: "Projects" }));
    await act(async () => {});

    expect(api.reviewQueue).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("1 awaiting review")).toBeInTheDocument();
  });
});

describe("the mobile menu", () => {
  it("names what the toggle controls", async () => {
    await draw();

    const toggle = screen.getByRole("button", { name: "Toggle navigation menu" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "mobile-nav");
  });

  it("reveals the element it says it controls", async () => {
    await draw();

    fireEvent.click(screen.getByRole("button", { name: "Toggle navigation menu" }));

    expect(document.getElementById("mobile-nav")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Toggle navigation menu" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("leaves focus on the toggle, which is what a disclosure should do", async () => {
    await draw();
    const toggle = screen.getByRole("button", { name: "Toggle navigation menu" });

    await userEvent.click(toggle);

    expect(toggle).toHaveFocus();
  });

  it("closes itself when the user navigates", async () => {
    await draw();
    fireEvent.click(screen.getByRole("button", { name: "Toggle navigation menu" }));
    const nav = document.getElementById("mobile-nav");

    await userEvent.click(within(nav).getByRole("link", { name: "Calendar" }));
    await act(async () => {});

    expect(document.getElementById("mobile-nav")).not.toBeInTheDocument();
  });

  it("marks the collapsed menu when something is waiting behind it", async () => {
    api.reviewQueue.mockResolvedValue([{ id: 1 }]);

    await draw();

    const toggle = screen.getByRole("button", { name: "Toggle navigation menu" });
    // Decorative: the count itself is on the tab inside, which is announced.
    expect(toggle.querySelector('[aria-hidden="true"].rounded-full')).not.toBeNull();
  });
});

describe("the account", () => {
  it("shows who is signed in", async () => {
    await draw();

    expect(screen.getByText("writer@example.com")).toBeInTheDocument();
  });

  it("signs out, and leaves the page it signed out of", async () => {
    await draw("/calendar");

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await act(async () => {});

    expect(logout).toHaveBeenCalled();
    expect(screen.getByTestId("where")).toHaveTextContent("/login");
  });

  it("signs out from the mobile menu too, and not by a different route", async () => {
    // Two Sign out buttons in the markup — one in the header for wide screens,
    // one at the foot of the mobile menu — each with its own copy of the same
    // two-line handler. The second was never clicked by anything, and a
    // sign-out that clears the session without leaving the page is a signed-out
    // user looking at their own data until they navigate.
    await draw("/calendar");
    fireEvent.click(screen.getByRole("button", { name: "Toggle navigation menu" }));
    const nav = within(document.getElementById("mobile-nav"));

    await userEvent.click(nav.getByRole("button", { name: "Sign out" }));
    await act(async () => {});

    expect(logout).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("where")).toHaveTextContent("/login");
  });
});
