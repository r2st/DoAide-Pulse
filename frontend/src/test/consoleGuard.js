/**
 * Make `console.error` and `console.warn` fail the test that caused them.
 *
 * The counterpart to `filterwarnings = ["error"]` on the Python side, and for
 * the same reason. React reports the things it cannot throw over — an update
 * outside `act`, a key missing from a list, a controlled input that went
 * uncontrolled, a router behaviour about to change under you — by writing to
 * the console, where a green suite scrolls straight past them. Herald's own
 * source calls neither method, so anything arriving here came from React, the
 * router or a library, and is worth reading.
 *
 * The wrapper records rather than throws. Throwing from inside `console.error`
 * would be louder, but React calls it *during a render*, where the throw can be
 * caught by an error boundary — Herald has one — and vanish exactly in the case
 * the guard was installed for. Recording always survives; `release()` hands the
 * messages back and the caller fails the test with them.
 *
 * A test that means to provoke console output spies on it
 * (`vi.spyOn(console, "error")`); the spy replaces this wrapper for the
 * duration and `release()` leaves it alone.
 */

/** The two methods that mean "something is wrong", as opposed to `log`/`info`. */
export const GUARDED_METHODS = ["error", "warn"];

/**
 * Render one console argument as text for the failure message.
 *
 * Errors give up their stack, strings pass through, and everything else is
 * JSON — `String(obj)` would flatten the useful half of a React warning to
 * `[object Object]`. React also passes fibers and DOM nodes, which are
 * circular, so JSON is the attempt and `String` the fallback.
 *
 * @param {unknown} arg
 * @returns {string}
 */
export function describeArg(arg) {
  if (arg instanceof Error) return arg.stack ?? arg.message;
  if (typeof arg === "string") return arg;
  try {
    return JSON.stringify(arg) ?? String(arg);
  } catch {
    return String(arg);
  }
}

/**
 * Wrap the guarded console methods for one test.
 *
 * @param {object} [options]
 * @param {string} [options.testName] Named in the failure message, because the
 *   stack of a React warning points into React and says nothing about which
 *   test rendered it.
 * @param {Console} [options.target] The console to wrap; injectable so the
 *   guard's own tests do not have to fight the installed one.
 * @param {(fn: unknown) => boolean} [options.isMock] Used by `release` to tell
 *   a test's deliberate spy from this wrapper. Defaults to "never a mock",
 *   which suits everything but the real vitest wiring in `setup.js`.
 * @returns {{release: () => string[]}} `release` restores the console and
 *   returns every message captured, oldest first.
 */
export function installConsoleGuard({
  testName = "test",
  target = console,
  isMock = () => false,
} = {}) {
  const captured = [];
  const originals = {};

  for (const method of GUARDED_METHODS) {
    originals[method] = target[method];
    target[method] = (...args) => {
      captured.push(
        `Unexpected console.${method} during "${testName}": ` +
          args.map(describeArg).join(" "),
      );
    };
  }

  return {
    release() {
      for (const method of GUARDED_METHODS) {
        // A spy the test installed itself is the documented escape hatch, and
        // it may outlive this call — `vi.restoreAllMocks` runs on its own
        // schedule. Overwriting it here would put our wrapper back underneath
        // and leak it into the next test.
        if (!isMock(target[method])) {
          target[method] = originals[method];
        }
      }
      return captured.slice();
    },
  };
}
