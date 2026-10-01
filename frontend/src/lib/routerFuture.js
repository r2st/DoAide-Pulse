/**
 * React Router v7 behaviour, now the version actually installed.
 *
 * Originally these were v6 opt-ins: router 6.30 printed a console warning per
 * flag per router it mounted, which in the test suite was two lines for every
 * page test — several hundred lines of stderr that a real warning would have
 * had to compete with. Taking the new behaviour was the point of the notice.
 *
 * The upgrade to v7 has since happened (for GHSA-wrjc-x8rr-h8h6 and
 * GHSA-337j-9hxr-rhxg, which have no fix on the 6.x line), so both flags now
 * describe the default and passing them changes nothing. They are kept because
 * the object is also what pins app and tests to one router configuration, and
 * because the descriptions below are the record of what that configuration is.
 *
 * - `v7_startTransition` wraps router state updates in `React.startTransition`,
 *   so a navigation that suspends keeps the current screen up instead of
 *   flashing the nearest fallback.
 * - `v7_relativeSplatPath` fixes relative link resolution inside a splat route
 *   (`path="*"`), where v6 resolves against the matched splat rather than the
 *   route that declared it. Pulse has one splat — the catch-all redirect in
 *   `App.jsx` — so this is a no-op today and a trap disarmed for the next one.
 *
 * Exported as a single frozen object so the app and every test mount the same
 * router configuration: a test that opts out of a flag the app opts into is
 * testing a router the user never runs.
 */
export const ROUTER_FUTURE = Object.freeze({
  v7_startTransition: true,
  v7_relativeSplatPath: true,
});

export default ROUTER_FUTURE;
