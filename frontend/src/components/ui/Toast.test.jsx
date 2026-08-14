/**
 * The toasts themselves, rather than a stand-in for them.
 *
 * Every page test in this suite mocks `useToast` — correctly, because a page
 * test asserting that saving a project shows a toast should not also be a test
 * of how long the toast stays up. The consequence is that until this file
 * existed, nothing rendered a real one: the auto-dismiss timers, the stacking,
 * the choice of `alert` over `status` for an error, and the promise that a
 * toast raised outside a provider fails loudly rather than silently doing
 * nothing were all unexercised.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider, useToast } from "./Toast";
import { whileCaught } from "../../test/caught";

/** A button per tone, so a test can raise one by clicking. */
function Raiser({ onReady }) {
  const toast = useToast();
  if (onReady) onReady(toast);
  return (
    <div>
      <button onClick={() => toast.success("Project saved")}>save</button>
      <button onClick={() => toast.error("Could not save")}>fail</button>
      <button onClick={() => toast.info("Scan started")}>note</button>
    </div>
  );
}

function draw() {
  let toast = null;
  const result = render(
    <ToastProvider>
      <Raiser
        onReady={(t) => {
          toast = t;
        }}
      />
    </ToastProvider>,
  );
  return { ...result, get toast() {
    return toast;
  } };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("raising one", () => {
  it("shows the message", () => {
    const { toast } = draw();
    act(() => void toast.success("Project saved"));

    expect(screen.getByText("Project saved")).toBeInTheDocument();
  });

  it("stacks several at once, oldest first", () => {
    const { toast } = draw();
    act(() => {
      toast.success("first");
      toast.info("second");
      toast.error("third");
    });

    const messages = screen
      .getAllByText(/first|second|third/)
      .map((node) => node.textContent);
    expect(messages).toEqual(["first", "second", "third"]);
  });

  it("ignores an empty message rather than flashing a blank box", () => {
    // `toast.error(err.message)` is the common call, and `err.message` is
    // empty often enough to matter.
    const { container, toast } = draw();
    let id;
    act(() => {
      id = toast.error("");
    });

    expect(id).toBeUndefined();
    expect(container.querySelectorAll("[role='alert'], [role='status']")).toHaveLength(
      0,
    );
  });
});

describe("how it is announced", () => {
  it("reads an error as an alert", () => {
    const { toast } = draw();
    act(() => void toast.error("Could not save"));

    expect(screen.getByRole("alert")).toHaveTextContent("Could not save");
  });

  it("reads the quieter tones as status, not as alerts", () => {
    // An alert interrupts. "Project saved" is not worth interrupting for.
    const { toast } = draw();
    act(() => {
      toast.success("Project saved");
      toast.info("Scan started");
    });

    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the region polite so a toast never cuts off a reader", () => {
    const { container } = draw();
    expect(container.querySelector("[aria-live]")).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });
});

describe("dismissing", () => {
  it("clears a success toast on its own", () => {
    const { toast } = draw();
    act(() => void toast.success("Project saved"));

    act(() => void vi.advanceTimersByTime(3499));
    expect(screen.getByText("Project saved")).toBeInTheDocument();

    act(() => void vi.advanceTimersByTime(1));
    expect(screen.queryByText("Project saved")).not.toBeInTheDocument();
  });

  it("leaves an error up for longer than a success", () => {
    // An error is the one the reader most needs time to finish reading.
    const { toast } = draw();
    act(() => void toast.error("Could not save"));

    act(() => void vi.advanceTimersByTime(3500));
    expect(screen.getByText("Could not save")).toBeInTheDocument();

    act(() => void vi.advanceTimersByTime(2500));
    expect(screen.queryByText("Could not save")).not.toBeInTheDocument();
  });

  it("honours an explicit duration", () => {
    const { toast } = draw();
    act(() => void toast.info("Scan started", { duration: 100 }));

    act(() => void vi.advanceTimersByTime(100));
    expect(screen.queryByText("Scan started")).not.toBeInTheDocument();
  });

  it("keeps a toast up indefinitely when asked for no duration at all", () => {
    const { toast } = draw();
    act(() => void toast.error("Publishing…", { duration: 0 }));

    act(() => void vi.advanceTimersByTime(60_000));
    expect(screen.getByText("Publishing…")).toBeInTheDocument();
  });

  it("can be dismissed by hand before its time is up", () => {
    const { toast } = draw();
    act(() => void toast.error("Could not save"));

    act(() =>
      void screen.getByRole("button", { name: "Dismiss notification" }).click(),
    );
    expect(screen.queryByText("Could not save")).not.toBeInTheDocument();
  });

  it("dismisses only the one that was clicked", () => {
    const { toast } = draw();
    act(() => {
      toast.info("first");
      toast.info("second");
    });

    act(() =>
      void screen.getAllByRole("button", { name: "Dismiss notification" })[0].click(),
    );

    expect(screen.queryByText("first")).not.toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it("cancels the timer of a toast dismissed by hand", () => {
    // Not for what the timer would do — dismissing twice is harmless — but
    // because it would otherwise still be pending. A page that raises a toast
    // per save leaves one live timer per dismissed toast behind it, each
    // holding the provider's state setter until it fires.
    const { toast } = draw();
    act(() => void toast.success("Project saved"));
    expect(vi.getTimerCount()).toBe(1);

    act(() =>
      void screen.getByRole("button", { name: "Dismiss notification" }).click(),
    );

    expect(vi.getTimerCount()).toBe(0);
    expect(screen.queryByText("Project saved")).not.toBeInTheDocument();
  });
});

describe("outside a provider", () => {
  it("throws rather than dropping the message on the floor", async () => {
    // A silently missing provider means a confirmation the user never sees for
    // an action that did happen. Better to fail at the first render.
    await whileCaught(() => {
      expect(() => render(<Raiser />)).toThrow(/within a ToastProvider/);
    });
  });
});
