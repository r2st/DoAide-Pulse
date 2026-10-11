import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import trackEvent from "./trackEvent";

describe("trackEvent", () => {
  let originalUmami;

  beforeEach(() => {
    originalUmami = window.umami;
  });

  afterEach(() => {
    window.umami = originalUmami;
  });

  it("calls window.umami.track when umami is loaded", () => {
    window.umami = { track: vi.fn() };
    trackEvent("signup", { source: "landing" });
    expect(window.umami.track).toHaveBeenCalledWith("signup", { source: "landing" });
  });

  it("no-ops when umami is not loaded", () => {
    window.umami = undefined;
    expect(() => trackEvent("signup")).not.toThrow();
  });

  it("swallows errors from umami.track", () => {
    window.umami = {
      track: () => {
        throw new Error("boom");
      },
    };
    expect(() => trackEvent("signup")).not.toThrow();
  });

  it("works without data argument", () => {
    window.umami = { track: vi.fn() };
    trackEvent("logout");
    expect(window.umami.track).toHaveBeenCalledWith("logout", undefined);
  });
});
