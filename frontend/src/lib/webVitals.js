import trackEvent from "./trackEvent";

export default function reportWebVitals() {
  if (typeof window === "undefined" || !("PerformanceObserver" in window)) return;

  try {
    const navEntry = performance.getEntriesByType("navigation")[0];
    if (navEntry) {
      trackEvent("web-vitals", {
        ttfb: Math.round(navEntry.responseStart - navEntry.requestStart),
        dom_load: Math.round(navEntry.domContentLoadedEventEnd - navEntry.startTime),
        page_load: Math.round(navEntry.loadEventEnd - navEntry.startTime),
      });
    }
  } catch {
    // Best-effort only.
  }

  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        trackEvent("web-vitals", {
          metric: entry.name,
          value: Math.round(entry.startTime),
        });
      }
    }).observe({ type: "largest-contentful-paint", buffered: true });
  } catch {
    // LCP observer not supported.
  }

  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        trackEvent("web-vitals", {
          metric: "CLS",
          value: Math.round(entry.value * 1000),
        });
      }
    }).observe({ type: "layout-shift", buffered: true });
  } catch {
    // CLS observer not supported.
  }
}
