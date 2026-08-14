/**
 * The v7 flags, and the two things that make them worth a test file.
 *
 * First: the app and the suite have to mount the same router. Router options
 * change behaviour — `v7_startTransition` changes when React commits a
 * navigation — so a suite that leaves them off is exercising a router the user
 * does not run, which is the failure mode this whole module exists to close.
 *
 * Second: `future` is an ordinary prop with no validation behind it. A typo in
 * a flag name is not an error, it is silence — the old behaviour stays, the
 * console warning comes back, and the only signal is a line of stderr in a
 * suite that now fails on those. So the names are pinned literally rather than
 * read back from the object under test.
 */
import { describe, expect, it } from "vitest";
import { ROUTER_FUTURE } from "./routerFuture";

describe("ROUTER_FUTURE", () => {
  it("names both flags exactly as react-router spells them", () => {
    expect(ROUTER_FUTURE).toEqual({
      v7_startTransition: true,
      v7_relativeSplatPath: true,
    });
  });

  it("is frozen, so a caller cannot flip a flag for everyone else", () => {
    expect(Object.isFrozen(ROUTER_FUTURE)).toBe(true);
  });

  it("keeps its value when something tries to mutate it", () => {
    // Frozen objects fail silently outside strict mode and throw inside it;
    // ES modules are strict, so this is the throwing case. Either way the
    // assertion afterwards is the one that matters.
    expect(() => {
      ROUTER_FUTURE.v7_startTransition = false;
    }).toThrow();
    expect(ROUTER_FUTURE.v7_startTransition).toBe(true);
  });
});
