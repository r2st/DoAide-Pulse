import { vi } from "vitest";

/**
 * Run something that is expected to make React catch an error, with React's own
 * report of it silenced for exactly that long.
 *
 *   await whileCaught(() => render(<ErrorBoundary><Boom /></ErrorBoundary>));
 *   expect(screen.getByRole("alert")).toBeInTheDocument();
 *
 * An error that reaches a boundary is one React always writes to
 * `console.error`, and the guard in `./setup.js` turns a stray write into a
 * failed test. Spying is the escape hatch that guard documents; doing it here
 * rather than in an `afterEach` means the spy is gone before the assertions
 * run, so it cannot go on swallowing an unrelated warning from the rest of the
 * test — which is the failure mode a file-wide spy has.
 *
 * Awaits `fn`, so a callback that clicks with `user-event` works too.
 *
 * @template T
 * @param {() => T | Promise<T>} fn
 * @returns {Promise<T>} Whatever `fn` returned — usually a render result.
 */
export async function whileCaught(fn) {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    return await fn();
  } finally {
    spy.mockRestore();
  }
}

export default whileCaught;
