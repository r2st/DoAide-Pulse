import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import reportWebVitals from "./webVitals";

describe("reportWebVitals", () => {
  let originalUmami;

  beforeEach(() => {
    originalUmami = window.umami;
    window.umami = { track: vi.fn() };
  });

  afterEach(() => {
    window.umami = originalUmami;
  });

  it("sends navigation timing to Umami", () => {
    vi.spyOn(performance, "getEntriesByType").mockReturnValue([
      { responseStart: 50, requestStart: 10, domContentLoadedEventEnd: 200, startTime: 0, loadEventEnd: 400 },
    ]);

    reportWebVitals();

    expect(window.umami.track).toHaveBeenCalledWith("web-vitals", {
      ttfb: 40,
      dom_load: 200,
      page_load: 400,
    });
  });

  it("does not throw when PerformanceObserver is unavailable", () => {
    const orig = window.PerformanceObserver;
    delete window.PerformanceObserver;
    vi.spyOn(performance, "getEntriesByType").mockReturnValue([]);
    expect(() => reportWebVitals()).not.toThrow();
    window.PerformanceObserver = orig;
  });
});
