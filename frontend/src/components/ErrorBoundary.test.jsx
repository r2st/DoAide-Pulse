/**
 * The boundary that keeps one bad render from blanking the whole app.
 *
 * Covers what it catches (renders, effects, deep descendants), what it puts up
 * instead, the three ways a caught error clears — the retry button, a changed
 * `resetKey`, a navigation — and the containment property that is the whole
 * reason for having more than one of them.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { MemoryRouter, Route, Routes, Link } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ErrorBoundary, { ErrorFallback, RouteErrorBoundary } from "./ErrorBoundary";
import { ROUTER_FUTURE } from "../lib/routerFuture";
import { whileCaught } from "../test/caught";

/** Throws on every render. */
function Boom({ message = "kaboom" }) {
  throw new Error(message);
}

/**
 * Throws for as long as `state.broken` says to, so a test can decide when the
 * retry succeeds.
 *
 * Deliberately not "throws on the first render only": React re-invokes a
 * component that threw a second time in development, to collect the component
 * stack it puts in the console, and a counter-based fake would use up its one
 * failure on that invisible second pass.
 */
function ThrowsWhile({ state }) {
  if (state.broken) throw new Error("transient");
  return <p>recovered</p>;
}

const fallbackText = /this section stopped rendering/i;

describe("catching", () => {
  it("renders its children untouched when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>all fine</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("all fine")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("replaces the subtree that threw with the fallback", async () => {
    await whileCaught(() =>
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(fallbackText);
  });

  it("keeps the error's own message, because 'an error occurred' is not a report", async () => {
    await whileCaught(() =>
      render(
        <ErrorBoundary>
          <Boom message="cover_image_url of undefined" />
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByText("cover_image_url of undefined")).toBeInTheDocument();
  });

  it("catches a throw from a descendant, not just a direct child", async () => {
    const Deep = () => (
      <section>
        <div>
          <Boom />
        </div>
      </section>
    );

    await whileCaught(() =>
      render(
        <ErrorBoundary>
          <Deep />
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(fallbackText);
  });

  it("catches a throw from an effect, which runs after the render it belongs to", async () => {
    function ThrowsOnMount() {
      useEffect(() => {
        throw new Error("late");
      }, []);
      return <p>mounted</p>;
    }

    await whileCaught(() =>
      render(
        <ErrorBoundary>
          <ThrowsOnMount />
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByText("late")).toBeInTheDocument();
    expect(screen.queryByText("mounted")).not.toBeInTheDocument();
  });

  it("uses the title it was given", async () => {
    await whileCaught(() =>
      render(
        <ErrorBoundary title="The chart broke">
          <Boom />
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByText("The chart broke")).toBeInTheDocument();
  });
});

describe("containment", () => {
  it("leaves a sibling boundary's subtree rendering", async () => {
    await whileCaught(() =>
      render(
        <div>
          <ErrorBoundary>
            <Boom />
          </ErrorBoundary>
          <ErrorBoundary>
            <p>still here</p>
          </ErrorBoundary>
        </div>,
      ),
    );

    expect(screen.getByText("still here")).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("leaves everything outside it alone — the point of not putting one at the root", async () => {
    await whileCaught(() =>
      render(
        <div>
          <nav>Navigation</nav>
          <ErrorBoundary>
            <Boom />
          </ErrorBoundary>
        </div>,
      ),
    );

    expect(screen.getByText("Navigation")).toBeInTheDocument();
  });
});

describe("reporting", () => {
  it("hands onError the error, the component stack, and the section name", async () => {
    const onError = vi.fn();

    await whileCaught(() =>
      render(
        <ErrorBoundary onError={onError} section="analytics">
          <Boom message="reported" />
        </ErrorBoundary>,
      ),
    );

    expect(onError).toHaveBeenCalledTimes(1);
    const [error, info, section] = onError.mock.calls[0];
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toBe("reported");
    expect(info.componentStack).toContain("Boom");
    expect(section).toBe("analytics");
  });

  it("does not call onError when nothing throws", () => {
    const onError = vi.fn();

    render(
      <ErrorBoundary onError={onError}>
        <p>fine</p>
      </ErrorBoundary>,
    );

    expect(onError).not.toHaveBeenCalled();
  });
});

describe("recovering", () => {
  it("re-renders the children when Try again is pressed", async () => {
    const state = { broken: true };

    await whileCaught(() =>
      render(
        <ErrorBoundary>
          <ThrowsWhile state={state} />
        </ErrorBoundary>,
      ),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    state.broken = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("recovered")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the fallback again — not a blank page — when the throw is deterministic", async () => {
    await whileCaught(async () => {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    expect(screen.getByRole("alert")).toHaveTextContent(fallbackText);
  });

  it("clears the error when resetKey changes", async () => {
    const state = { broken: true };
    const draw = (resetKey) => (
      <ErrorBoundary resetKey={resetKey}>
        <ThrowsWhile state={state} />
      </ErrorBoundary>
    );

    const { rerender } = await whileCaught(() => render(draw("a")));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    state.broken = false;
    rerender(draw("b"));

    expect(screen.getByText("recovered")).toBeInTheDocument();
  });

  it("holds the fallback across a re-render that keeps the same resetKey", async () => {
    const state = { broken: true };
    const draw = () => (
      <ErrorBoundary resetKey="same">
        <ThrowsWhile state={state} />
      </ErrorBoundary>
    );

    const { rerender } = await whileCaught(() => render(draw()));

    // Repairable now — but an unrelated re-render is not the signal to retry,
    // or a parent that renders on a timer would loop through the same throw.
    state.broken = false;
    rerender(draw());

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("recovered")).not.toBeInTheDocument();
  });
});

describe("a custom fallback", () => {
  it("is given the error and a reset that works", async () => {
    const state = { broken: true };

    await whileCaught(() =>
      render(
        <ErrorBoundary
          fallback={({ error, reset }) => (
            <div>
              <p>custom: {error.message}</p>
              <button onClick={reset}>Retry now</button>
            </div>
          )}
        >
          <ThrowsWhile state={state} />
        </ErrorBoundary>,
      ),
    );
    expect(screen.getByText("custom: transient")).toBeInTheDocument();
    // The default panel is gone, not merely hidden behind the custom one.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    state.broken = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));

    expect(screen.getByText("recovered")).toBeInTheDocument();
  });
});

describe("RouteErrorBoundary", () => {
  function app() {
    return (
      <MemoryRouter future={ROUTER_FUTURE} initialEntries={["/broken"]}>
        <nav>
          <Link to="/works">Go elsewhere</Link>
        </nav>
        <RouteErrorBoundary>
          <Routes>
            <Route path="/broken" element={<Boom message="this page" />} />
            <Route path="/works" element={<p>a working page</p>} />
          </Routes>
        </RouteErrorBoundary>
      </MemoryRouter>
    );
  }

  it("clears the error on navigation, so one bad page does not follow the user", async () => {
    await whileCaught(() => render(app()));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    // The link is outside the boundary on purpose: if it were inside, the
    // fallback would have replaced the only way out.
    fireEvent.click(screen.getByRole("link", { name: "Go elsewhere" }));

    expect(screen.getByText("a working page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("passes through props, so a caller can still name the section", async () => {
    const onError = vi.fn();

    await whileCaught(() =>
      render(
        <MemoryRouter future={ROUTER_FUTURE}>
          <RouteErrorBoundary title="Page failed" section="page" onError={onError}>
            <Boom />
          </RouteErrorBoundary>
        </MemoryRouter>,
      ),
    );

    expect(screen.getByText("Page failed")).toBeInTheDocument();
    expect(onError.mock.calls[0][2]).toBe("page");
  });
});

describe("ErrorFallback on its own", () => {
  it("offers no retry when there is nothing to retry with", () => {
    render(<ErrorFallback error={new Error("no reset")} />);

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders without an error object at all", () => {
    render(<ErrorFallback />);

    expect(screen.getByRole("alert")).toHaveTextContent(fallbackText);
  });

  it("defaults to a title that says what happened without blaming the user", () => {
    render(<ErrorFallback error={new Error("x")} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });
});
