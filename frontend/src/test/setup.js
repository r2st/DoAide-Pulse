import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { installConsoleGuard } from "./consoleGuard";

/**
 * A working `window.localStorage`, because this environment has none.
 *
 * Node 22 exposes a native `localStorage` gated behind `--localstorage-file`,
 * and it shadows the one jsdom would otherwise provide — so `window.localStorage`
 * is genuinely undefined under vitest here. Real browsers have it, so a component
 * test running without one is exercising a situation no user is in.
 *
 * Installed fresh before each test: storage that leaks between tests turns an
 * ordering change into a mystery failure.
 *
 * `length` and `key(i)` are index-ordered over insertion, which is what
 * `draftStore.prune` iterates over.
 */
function makeStorage() {
  let map = new Map();
  return {
    get length() {
      return map.size;
    },
    key: (index) => [...map.keys()][index] ?? null,
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => void map.set(key, String(value)),
    removeItem: (key) => void map.delete(key),
    clear: () => void (map = new Map()),
  };
}

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    value: makeStorage(),
    configurable: true,
    writable: true,
  });
});

/**
 * Wire the console guard into every test.
 *
 * The guard itself — and the reasoning for it — lives in `./consoleGuard`, so
 * that it can be tested as an ordinary module rather than only through its own
 * side effects on the runner.
 */
let guard = null;

beforeEach((context) => {
  guard = installConsoleGuard({
    testName: context.task?.name ?? "test",
    isMock: vi.isMockFunction,
  });
});

afterEach(() => {
  const messages = guard.release();
  guard = null;
  if (messages.length > 0) {
    throw new Error(messages.join("\n"));
  }
});
