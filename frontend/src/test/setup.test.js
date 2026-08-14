/**
 * The console guard — the frontend's `-W error`.
 *
 * React and react-router report the problems they cannot throw over by writing
 * to the console: an update outside `act`, a missing list key, a controlled
 * input that went uncontrolled, a router behaviour about to change. A suite
 * that only checks its own assertions goes green through every one of them.
 *
 * The guard is exercised here against an injected fake console rather than the
 * live one, for the obvious reason: the real guard is installed around this
 * test too, and a test that swaps out the thing testing it proves nothing. The
 * one case that does need the installed guard — that it is installed at all —
 * is the last test in the file.
 */
import { describe, expect, it, vi } from "vitest";
import {
  GUARDED_METHODS,
  describeArg,
  installConsoleGuard,
} from "./consoleGuard";

/** A console with only the methods the guard touches, plus a call log. */
function fakeConsole() {
  const calls = [];
  return {
    calls,
    error: (...args) => calls.push(["error", ...args]),
    warn: (...args) => calls.push(["warn", ...args]),
    log: (...args) => calls.push(["log", ...args]),
  };
}

describe("describeArg", () => {
  it("gives an Error its stack, not [object Object]", () => {
    const err = new Error("kaboom");
    expect(describeArg(err)).toContain("kaboom");
  });

  it("falls back to the message when an Error carries no stack", () => {
    const err = new Error("stackless");
    err.stack = undefined;
    expect(describeArg(err)).toBe("stackless");
  });

  it("passes a string through unchanged", () => {
    expect(describeArg("plain")).toBe("plain");
  });

  it("renders an object as JSON, which is where the detail is", () => {
    expect(describeArg({ id: 3, kind: "warning" })).toBe(
      '{"id":3,"kind":"warning"}',
    );
  });

  it("survives a circular structure — React passes fibers and DOM nodes", () => {
    const circular = { name: "fiber" };
    circular.self = circular;
    expect(describeArg(circular)).toBe("[object Object]");
  });

  it("names undefined rather than returning it, since JSON.stringify does not", () => {
    expect(describeArg(undefined)).toBe("undefined");
  });
});

describe("installConsoleGuard", () => {
  it("captures a console.error instead of letting it through", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ testName: "a test", target });

    target.error("something is wrong");

    expect(guard.release()).toEqual([
      'Unexpected console.error during "a test": something is wrong',
    ]);
    expect(target.calls).toEqual([]);
  });

  it("captures console.warn too", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ testName: "a test", target });

    target.warn("careful");

    expect(guard.release()[0]).toMatch(/Unexpected console\.warn/);
  });

  it("does not throw from inside the console call", () => {
    // Deliberate: React calls console.error mid-render, where a throw can be
    // caught by an error boundary and lost. Recording always survives.
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });

    expect(() => target.error("boom")).not.toThrow();

    guard.release();
  });

  it("joins every argument into one message", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });

    target.error("missing key for", { id: 3 });

    expect(guard.release()[0]).toBe(
      'Unexpected console.error during "test": missing key for {"id":3}',
    );
  });

  it("keeps captures in the order they happened", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });

    target.error("first");
    target.warn("second");
    target.error("third");

    expect(guard.release().map((m) => m.split(": ").pop())).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("says which test it was, because a React stack will not", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ testName: "renders the queue", target });

    target.error("boom");

    expect(guard.release()[0]).toContain('during "renders the queue"');
  });

  it("defaults the test name rather than printing undefined", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });

    target.error("boom");

    expect(guard.release()[0]).toContain('during "test"');
  });

  it("returns an empty list when the test was quiet", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });

    expect(guard.release()).toEqual([]);
  });

  it("restores the original methods on release", () => {
    const target = fakeConsole();
    const before = { error: target.error, warn: target.warn };

    installConsoleGuard({ target }).release();

    expect(target.error).toBe(before.error);
    expect(target.warn).toBe(before.warn);
  });

  it("leaves a deliberate spy in place instead of restoring over it", () => {
    // The documented escape hatch: a test that asserts on console output spies
    // on it. Putting the original back underneath would leave the guard's
    // wrapper installed for the next test.
    const target = fakeConsole();
    const guard = installConsoleGuard({
      target,
      isMock: (fn) => fn.isSpy === true,
    });
    const spy = Object.assign(() => {}, { isSpy: true });
    target.error = spy;

    guard.release();

    expect(target.error).toBe(spy);
    expect(target.warn).not.toBe(spy);
  });

  it("hands back a copy, so a caller cannot edit the record", () => {
    const target = fakeConsole();
    const guard = installConsoleGuard({ target });
    target.error("boom");

    const first = guard.release();
    first.push("invented");

    expect(guard.release()).toHaveLength(1);
  });

  it("leaves console.log alone — it is output, not a distress signal", () => {
    const target = fakeConsole();
    const original = target.log;
    const guard = installConsoleGuard({ target });

    expect(target.log).toBe(original);
    target.log("just output");

    expect(guard.release()).toEqual([]);
    expect(target.calls).toEqual([["log", "just output"]]);
  });

  it("guards error and warn, and nothing else", () => {
    expect(GUARDED_METHODS).toEqual(["error", "warn"]);
  });
});

describe("the guard as wired into the suite", () => {
  it("is installed on the live console around this very test", () => {
    // Everything above runs against a fake console, which would go on passing
    // if `setup.js` stopped installing the guard entirely. This is the check
    // that it is actually wired in — and it cannot be made by *calling*
    // `console.error`, because that would fail this test in `afterEach`.
    //
    // The tell is the name. Node's own `console.error` is a named function;
    // the guard's replacement is an arrow assigned through a computed member
    // (`target[method] = ...`), which JavaScript does not name.
    expect(typeof console.error).toBe("function");
    expect(console.error.name).toBe("");
    expect(console.warn.name).toBe("");
    // `log` is untouched, so it still carries its name — which is what makes
    // the two assertions above mean something.
    expect(console.log.name).toBe("log");
  });

  it("still lets a spy take over, which is the escape hatch tests are told to use", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    console.error("expected, and asserted on");
    expect(spy).toHaveBeenCalledWith("expected, and asserted on");
    spy.mockRestore();
  });
});
