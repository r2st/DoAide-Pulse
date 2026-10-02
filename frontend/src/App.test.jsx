/**
 * The route table, and where the error boundaries sit in it.
 *
 * The boundaries are the reason this file exists: their placement is the part
 * that can silently regress — moving one outside `Shell`, or dropping it from a
 * route — without any single component's own tests noticing. What is asserted
 * here is the user-visible consequence of the placement: a page that throws
 * still leaves a way out of it.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { ROUTER_FUTURE } from "./lib/routerFuture";
import { whileCaught } from "./test/caught";

vi.mock("./hooks/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("./lib/api", () => ({
  api: {
    reviewQueue: vi.fn(() => Promise.resolve([])),
    publicationQueue: vi.fn(() => Promise.resolve([])),
  },
  getToken: vi.fn(() => "token"),
  setToken: vi.fn(),
}));

// Stand-ins for the two pages this file navigates between. The real ones would
// drag their own fetches in and make a boundary test into a page test.
vi.mock("./pages/Dashboard", () => ({
  default: () => {
    throw new Error("dashboard exploded");
  },
}));

vi.mock("./pages/LandingPage", () => ({
  default: () => <p>landing</p>,
}));

vi.mock("./pages/Projects", () => ({
  default: () => <p>the projects page</p>,
}));

vi.mock("./pages/Login", () => ({
  default: () => {
    throw new Error("login exploded");
  },
}));

import { useAuth } from "./hooks/useAuth";

function signedIn(overrides = {}) {
  useAuth.mockReturnValue({
    user: { email: "writer@example.com" },
    loading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  });
}

/**
 * Mount the app at `path` and let the Shell's two queue counts land.
 *
 * Without the flush their `setState` arrives after the test body has finished,
 * outside `act`, and React says so on the console — which the guard in
 * `test/setup.js` correctly treats as a failure.
 */
async function draw(path) {
  const result = render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
  await settle();
  return result;
}

/** Let every promise already in flight resolve inside `act`. */
function settle() {
  return act(async () => {});
}

/** Follow a nav link, then let the refreshed queue counts land. */
async function clickNav(name) {
  fireEvent.click(screen.getByRole("link", { name }));
  await settle();
}

beforeEach(() => {
  signedIn();
});

describe("the gate", () => {
  it("holds a protected route until the session resolves", async () => {
    signedIn({ user: null, loading: true });

    await draw("/dashboard");

    expect(screen.getByText("Loading")).toBeInTheDocument();
  });

  it("sends a signed-out visitor to the login page", async () => {
    signedIn({ user: null, loading: false });

    // Login is mocked to throw, so its boundary's fallback is the proof that
    // the redirect happened and the login route rendered.
    await whileCaught(() => draw("/dashboard"));

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("login exploded")).toBeInTheDocument();
  });
});

describe("a page that throws", () => {
  it("leaves the navigation standing, so there is a way out of it", async () => {
    await whileCaught(() => draw("/dashboard"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      /this page stopped working/i,
    );
    // The chrome outside the boundary survived: wordmark, destinations, account.
    expect(screen.getByRole("link", { name: "Projects" })).toBeInTheDocument();
    expect(screen.getByText("writer@example.com")).toBeInTheDocument();
  });

  it("keeps the error's message, so the fallback is worth screenshotting", async () => {
    await whileCaught(() => draw("/dashboard"));

    expect(screen.getByText("dashboard exploded")).toBeInTheDocument();
  });

  it("recovers as soon as the user navigates away from it", async () => {
    await whileCaught(() => draw("/dashboard"));

    await clickNav("Projects");

    expect(screen.getByText("the projects page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("an unshelled route that throws", () => {
  it("centres its own fallback, having no chrome to sit under", async () => {
    signedIn({ user: null, loading: false });

    await whileCaught(() => draw("/login"));

    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    // Its own framing, not the Shell's: no navigation on this page at all.
    expect(screen.queryByRole("link", { name: "Projects" })).not.toBeInTheDocument();
    expect(alert.parentElement).toHaveClass("max-w-md");
  });
});

describe("the route table", () => {
  it("redirects an unknown path to the dashboard", async () => {
    // Reached the dashboard route: its stand-in throws, and the boundary that
    // catches it is the one inside Shell.
    await whileCaught(() => draw("/no-such-page"));

    expect(screen.getByRole("link", { name: "Projects" })).toBeInTheDocument();
    expect(screen.getByText("dashboard exploded")).toBeInTheDocument();
  });

  it("renders a protected page inside the Shell when it does not throw", async () => {
    await draw("/projects");

    expect(screen.getByText("the projects page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
  });
});
