/**
 * Which URL renders which page, and which of them need a session.
 *
 * `App.test.jsx` is about the error boundaries and mounts three routes to make
 * its point. The route table itself — eleven `<Route>` elements, each pairing a
 * path with an import — was never asserted, and it is exactly the kind of thing
 * that regresses without a stack trace. Two `element={...}` blocks swapped in a
 * copy-paste, or a page dropped from the wrong route during a rename, produces
 * an app that renders a real page at the wrong URL: no error, no failing
 * component test, just the Calendar under /publish.
 *
 * Every page is a stand-in here, for the same reason the other file uses them —
 * the real ones drag their own fetches in and turn a routing test into a page
 * test. What each stand-in renders is its own name, so an assertion that the
 * right page is on screen is a direct assertion about the pairing.
 *
 * Kept out of `App.test.jsx` because the mocks contradict: that file needs
 * Dashboard and Login to throw, and this one needs every page to render.
 */
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { ROUTER_FUTURE } from "./lib/routerFuture";

vi.mock("./hooks/useAuth", () => ({ useAuth: vi.fn() }));

vi.mock("./lib/api", () => ({
  api: {
    reviewQueue: vi.fn(() => Promise.resolve([])),
    publicationQueue: vi.fn(() => Promise.resolve([])),
  },
  getToken: vi.fn(() => "token"),
  setToken: vi.fn(),
}));

// One stand-in per page, each announcing which page it is. Written out rather
// than built by a helper: `vi.mock` factories are hoisted above every
// declaration in the file, so a shared `page()` is not yet defined when they
// run.
vi.mock("./pages/Analytics", () => ({ default: () => <p>page: Analytics</p> }));
vi.mock("./pages/Calendar", () => ({ default: () => <p>page: Calendar</p> }));
vi.mock("./pages/ContentEditor", () => ({ default: () => <p>page: ContentEditor</p> }));
vi.mock("./pages/ContentList", () => ({ default: () => <p>page: ContentList</p> }));
vi.mock("./pages/Dashboard", () => ({ default: () => <p>page: Dashboard</p> }));
vi.mock("./pages/LandingPage", () => ({ default: () => <p>page: LandingPage</p> }));
vi.mock("./pages/Login", () => ({ default: () => <p>page: Login</p> }));
vi.mock("./pages/PreviewPage", () => ({ default: () => <p>page: PreviewPage</p> }));
vi.mock("./pages/Projects", () => ({ default: () => <p>page: Projects</p> }));
vi.mock("./pages/Publish", () => ({ default: () => <p>page: Publish</p> }));
vi.mock("./pages/Settings", () => ({ default: () => <p>page: Settings</p> }));
vi.mock("./pages/Templates", () => ({ default: () => <p>page: Templates</p> }));
vi.mock("./pages/TemplatesGallery", () => ({ default: () => <p>page: TemplatesGallery</p> }));
vi.mock("./pages/NewsletterGallery", () => ({ default: () => <p>page: NewsletterGallery</p> }));
vi.mock("./pages/Triggers", () => ({ default: () => <p>page: Triggers</p> }));

import { useAuth } from "./hooks/useAuth";

function session({ user = { email: "writer@example.com" }, loading = false } = {}) {
  useAuth.mockReturnValue({
    user,
    loading,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
  });
}

/** Mount at `path` and let the Shell's two queue counts land inside `act`. */
async function draw(path) {
  const result = render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
  await act(async () => {});
  return result;
}

/** Every path that requires a session, and the page it renders. */
const PROTECTED = [
  ["/dashboard", "Dashboard"],
  ["/projects", "Projects"],
  ["/content", "ContentList"],
  ["/content/42", "ContentEditor"],
  ["/triggers", "Triggers"],
  ["/my-templates", "Templates"],
  ["/calendar", "Calendar"],
  ["/publish", "Publish"],
  ["/analytics", "Analytics"],
  ["/settings", "Settings"],
];

/** The ones that render without a session, and without the chrome. */
const PUBLIC = [
  ["/", "LandingPage"],
  ["/login", "Login"],
  ["/preview/abc123", "PreviewPage"],
  ["/templates", "TemplatesGallery"],
  ["/gallery", "NewsletterGallery"],
];

beforeEach(() => {
  session();
});

describe("the route table", () => {
  it("covers every route the app declares", () => {
    // A guard on the guards below, which are parametrised over these lists: a
    // route added to App.jsx and not to one of them is a route nothing here
    // checks, and the file would stay green while covering less of the table.
    expect(PROTECTED.length + PUBLIC.length).toBe(15);
  });

  it.each(PROTECTED)("renders %s as the %s page", async (path, name) => {
    await draw(path);

    expect(screen.getByText(`page: ${name}`)).toBeInTheDocument();
  });

  it.each(PUBLIC)("renders %s as the %s page", async (path, name) => {
    session({ user: null });

    await draw(path);

    expect(screen.getByText(`page: ${name}`)).toBeInTheDocument();
  });

  it("sends an unknown path to the dashboard", async () => {
    await draw("/no-such-page");

    expect(screen.getByText("page: Dashboard")).toBeInTheDocument();
  });

  it("does not match a nested unknown path to the page above it", async () => {
    // `/content/42/anything` is not a route. It must land on the dashboard
    // rather than on the editor, which is what a path-prefix match would do.
    await draw("/content/42/extra");

    expect(screen.getByText("page: Dashboard")).toBeInTheDocument();
    expect(screen.queryByText("page: ContentEditor")).not.toBeInTheDocument();
  });
});

describe("what needs a session", () => {
  it.each(PROTECTED)("turns a signed-out visitor away from %s", async (path) => {
    session({ user: null });

    await draw(path);

    expect(screen.getByText("page: LandingPage")).toBeInTheDocument();
  });

  it.each(PROTECTED)("waits rather than redirecting from %s mid-check", async (path) => {
    // The window between "no user yet" and "the token was fine". Redirecting
    // here would bounce every authenticated user to the login page on every
    // cold load, and then bounce them back once `/auth/me` answered.
    session({ user: null, loading: true });

    await draw(path);

    expect(screen.getByText("Loading")).toBeInTheDocument();
    expect(screen.queryByText("page: LandingPage")).not.toBeInTheDocument();
  });

  it.each(PUBLIC)("does not gate %s behind one", async (path, name) => {
    session({ user: null, loading: true });

    await draw(path);

    // Not even the loading screen: a shared preview link is opened by someone
    // with no account at all, and gating it on a session check would show them
    // the word "Loading" while a token they do not have fails to resolve.
    expect(screen.getByText(`page: ${name}`)).toBeInTheDocument();
    expect(screen.queryByText("Loading")).not.toBeInTheDocument();
  });
});

describe("which pages get the chrome", () => {
  it.each(PROTECTED)("wraps %s in the Shell", async (path) => {
    await draw(path);

    expect(screen.getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("writer@example.com")).toBeInTheDocument();
  });

  it.each(PUBLIC)("leaves %s unshelled", async (path) => {
    session({ user: null });

    await draw(path);

    expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("keeps the preview page unshelled even for a signed-in author", async () => {
    // The author previewing their own shared link must see what the recipient
    // sees, chrome included — otherwise the one person who checks the link is
    // the one person who cannot tell what it looks like.
    await draw("/preview/abc123");

    expect(screen.getByText("page: PreviewPage")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
  });
});
