/**
 * React Router v7 behaviour, opted into on v6.
 *
 * Router 6.30 prints a console warning per flag per router it mounts, which in
 * the test suite is two lines for every page test — several hundred lines of
 * stderr that a real warning would have had to compete with. Silencing them by
 * filtering console output would have kept the old behaviour and hidden the
 * notice; these flags take the new behaviour instead, which is the point of the
 * notice and what the v7 upgrade will do anyway.
 *
 * - `v7_startTransition` wraps router state updates in `React.startTransition`,
 *   so a navigation that suspends keeps the current screen up instead of
 *   flashing the nearest fallback.
 * - `v7_relativeSplatPath` fixes relative link resolution inside a splat route
 *   (`path="*"`), where v6 resolves against the matched splat rather than the
 *   route that declared it. Herald has one splat — the catch-all redirect in
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
