/**
 * The loader every page in Pulse reads its data through.
 *
 * Its loading/error/data cycle is asserted a hundred times over by the page
 * tests, but always through a page. The one thing they cannot reach is the
 * generation guard: a superseded request that resolves *after* the one that
 * replaced it must be thrown away. Nothing about a page makes that visible —
 * the symptom is the analytics window flicking back to 30 days a second after
 * you asked for 90, on a slow connection, sometimes — so it is pinned here,
 * where the two responses can be resolved in the wrong order on purpose.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useApi } from "./useApi";

/** A loader whose promises are resolved by hand, in whatever order. */
function deferred() {
  const calls = [];
  const loader = vi.fn(
    () =>
      new Promise((resolve, reject) => {
        calls.push({ resolve, reject });
      }),
  );
  return { loader, calls };
}

describe("the ordinary cycle", () => {
  it("starts out loading, with nothing to show", () => {
    const { loader } = deferred();
    const { result } = renderHook(() => useApi(loader, []));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("calls the loader once on mount", () => {
    const { loader } = deferred();
    renderHook(() => useApi(loader, []));

    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("hands over the result and stops loading", async () => {
    const { result } = renderHook(() =>
      useApi(() => Promise.resolve({ views: 12 }), []),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ views: 12 });
    expect(result.current.error).toBeNull();
  });

  it("keeps the message off a rejection, not the error object", async () => {
    // Callers render `error` straight into an `ErrorBanner`, so it has to be
    // a string.
    const { result } = renderHook(() =>
      useApi(() => Promise.reject(new Error("Service unavailable")), []),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Service unavailable");
    expect(result.current.data).toBeNull();
  });

  it("falls back to stringifying a rejection with no message", async () => {
    const { result } = renderHook(() =>
      useApi(() => Promise.reject("just a string"), []),
    );

    await waitFor(() => expect(result.current.error).toBe("just a string"));
  });
});

describe("reloading", () => {
  it("runs the loader again and clears a previous error", async () => {
    let fail = true;
    const { result } = renderHook(() =>
      useApi(
        () => (fail ? Promise.reject(new Error("down")) : Promise.resolve("up")),
        [],
      ),
    );
    await waitFor(() => expect(result.current.error).toBe("down"));

    fail = false;
    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.data).toBe("up");
    expect(result.current.error).toBeNull();
  });

  it("re-runs when the deps change", async () => {
    const loader = vi.fn((days) => Promise.resolve(days));
    const { result, rerender } = renderHook(
      ({ days }) => useApi(() => loader(days), [days]),
      { initialProps: { days: 30 } },
    );
    await waitFor(() => expect(result.current.data).toBe(30));

    rerender({ days: 90 });
    await waitFor(() => expect(result.current.data).toBe(90));
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("does not re-run for a render that changed nothing", async () => {
    const loader = vi.fn(() => Promise.resolve("x"));
    const { result, rerender } = renderHook(() => useApi(loader, []));
    await waitFor(() => expect(result.current.data).toBe("x"));

    rerender();
    rerender();

    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("lets a caller write the data back without a round trip", async () => {
    // `setData` is how the editor keeps the page in step with a PATCH it
    // already knows the result of.
    const { result } = renderHook(() => useApi(() => Promise.resolve({ n: 1 }), []));
    await waitFor(() => expect(result.current.data).toEqual({ n: 1 }));

    act(() => result.current.setData({ n: 2 }));
    expect(result.current.data).toEqual({ n: 2 });
  });
});

describe("a superseded request", () => {
  it("is ignored when it lands after the one that replaced it", async () => {
    const { loader, calls } = deferred();
    const { result, rerender } = renderHook(({ days }) => useApi(loader, [days]), {
      initialProps: { days: 30 },
    });

    rerender({ days: 90 });
    expect(calls).toHaveLength(2);

    // The 90-day answer arrives first, then the 30-day one it superseded.
    await act(async () => {
      calls[1].resolve("ninety");
      calls[0].resolve("thirty");
    });

    expect(result.current.data).toBe("ninety");
  });

  it("does not clear the loading flag on its way past", async () => {
    // A stale response that resolved `loading` would uncover a page whose real
    // request is still in flight.
    const { loader, calls } = deferred();
    const { result, rerender } = renderHook(({ days }) => useApi(loader, [days]), {
      initialProps: { days: 30 },
    });

    rerender({ days: 90 });
    await act(async () => {
      calls[0].resolve("thirty");
    });

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
  });

  it("does not surface an error from a request nobody is waiting for", async () => {
    const { loader, calls } = deferred();
    const { result, rerender } = renderHook(({ days }) => useApi(loader, [days]), {
      initialProps: { days: 30 },
    });

    rerender({ days: 90 });
    await act(async () => {
      calls[0].reject(new Error("the abandoned one failed"));
      calls[1].resolve("ninety");
    });

    expect(result.current.error).toBeNull();
    expect(result.current.data).toBe("ninety");
  });
});
