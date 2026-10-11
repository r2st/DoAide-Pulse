/**
 * Send a custom event to Umami analytics.
 *
 * Silently no-ops when Umami is not loaded (dev, ad-blocked, SSR), so callers
 * never need a guard. The data object is optional and kept small — Umami
 * discards values over 500 characters.
 */
export default function trackEvent(name, data) {
  try {
    if (typeof window !== "undefined" && window.umami) {
      window.umami.track(name, data);
    }
  } catch {
    // Analytics must never break the app.
  }
}
