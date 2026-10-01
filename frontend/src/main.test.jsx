/**
 * The entrypoint: the one file no other file imports.
 *
 * Which is exactly why it had no coverage. Everything below it is tested
 * through its own tree — `App` mounts under a `MemoryRouter` the tests supply,
 * `Shell` under a router the tests supply — so the file that decides what wraps
 * what in the shipped application was asserted by nothing at all.
 *
 * The ordering is the thing worth pinning, and it is stated as a claim in
 * `main.jsx`'s own comment: the boundary is *outside* the router and both
 * providers, so it catches what the per-route boundaries in `App.jsx` sit below
 * — a throw from the router itself, from `AuthProvider` while it resolves the
 * session, or from the toast layer. Nesting is invisible: move `ErrorBoundary`
 * one level in and every test in the suite still passes, while the real failure
 * it exists for — a throw during session resolution — goes back to being a
 * blank white page with nothing on it to click.
 *
 * So each test here replaces one layer with a component that throws, and asks
 * whether the fallback still appears. That is the only way to observe a
 * wrapping order from the outside.
 */
import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { whileCaught } from "./test/caught";

// Every layer main.jsx composes, stubbed to something inert and identifiable.
// The real ones would drag fetches, a session and the whole route table in, and
// this file is about the composition rather than about any of them.
const { authThrows, toastThrows } = vi.hoisted(() => ({
  authThrows: { value: false },
  toastThrows: { value: false },
}));

vi.mock("./App", () => ({
  default: () => <p>the application</p>,
}));

vi.mock("./hooks/useAuth", () => ({
  AuthProvider: ({ children }) => {
    if (authThrows.value) throw new Error("session lookup exploded");
    return children;
  },
}));

vi.mock("./components/ui/Toast", () => ({
  ToastProvider: ({ children }) => {
    if (toastThrows.value) throw new Error("toast layer exploded");
    return children;
  },
}));

/**
 * Import the module for its side effect, on a page that has a #root to fill.
 *
 * Inside `act`, because `createRoot().render()` schedules the work rather than
 * doing it: without this the import resolves before anything has rendered, and
 * React's report of a caught error lands after `whileCaught` has already put
 * `console.error` back.
 */
async function boot() {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  await act(async () => {
    await import("./main");
  });
  return root;
}

beforeEach(() => {
  vi.resetModules();
  authThrows.value = false;
  toastThrows.value = false;
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("starting the application", () => {
  it("renders into the element index.html provides", async () => {
    // `#root` is a contract with a file no test ever loads. Getting the id
    // wrong throws on `createRoot(null)` in the browser and nowhere else.
    const root = await boot();

    expect(await screen.findByText("the application")).toBeInTheDocument();
    expect(root).toContainElement(screen.getByText("the application"));
  });
});

describe("what the outermost boundary is outside of", () => {
  it("catches a throw from the provider that resolves the session", async () => {
    // The motivating case. `AuthProvider` runs before anything is on screen,
    // so a throw here is a blank page rather than a broken panel — and no
    // per-route boundary in App.jsx is mounted yet to catch it.
    authThrows.value = true;

    await whileCaught(boot);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Pulse failed to start")).toBeInTheDocument();
    expect(screen.queryByText("the application")).not.toBeInTheDocument();
  });

  it("catches a throw from the toast layer", async () => {
    toastThrows.value = true;

    await whileCaught(boot);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Pulse failed to start")).toBeInTheDocument();
  });

  it("offers the only recovery there is left at this level", async () => {
    // Deliberately no reset key: there is no navigation to recover with above
    // the router, so the fallback's own retry is the whole offer. A missing
    // one would leave the user with a titled dead end.
    authThrows.value = true;

    await whileCaught(boot);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Try again/);
  });
});
