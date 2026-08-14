/**
 * The modal behaviour eight hand-written dialogs claimed and none delivered.
 *
 * Every assertion here is about a keyboard or a screen reader, because that is
 * who was locked out: `aria-modal="true"` on a div that Tab walks straight out
 * of is worse than no attribute at all — it tells assistive technology the rest
 * of the page is unreachable while leaving it reachable.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import Dialog from "./Dialog";

/**
 * A dialog with something focusable behind it.
 *
 * The button outside is not decoration: with the dialog holding the only
 * focusable elements in the document, a browser wraps Tab around to the first
 * of them on its own, and every trap assertion below would pass against no trap
 * at all. `Behind the backdrop` is where focus goes if the trap is not working.
 */
function draw(props = {}, children = null) {
  return render(
    <>
      <button type="button">Behind the backdrop</button>
      <Dialog label="Publish" onClose={vi.fn()} {...props}>
        {children ?? (
          <>
            <h2>Publish</h2>
            <input aria-label="Slug" />
            <button type="button">Cancel</button>
          </>
        )}
      </Dialog>
    </>,
  );
}

describe("naming", () => {
  it("is a modal dialog with an accessible name", () => {
    draw();

    const dialog = screen.getByRole("dialog", { name: "Publish" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("renders a plain div when there is nothing to submit", () => {
    draw({}, <p>Read this and close it</p>);

    expect(document.querySelector("form")).toBeNull();
  });

  it("renders a form when given onSubmit, so Enter in a field submits", async () => {
    const onSubmit = vi.fn((event) => event.preventDefault());
    draw({ onSubmit });

    await userEvent.type(screen.getByLabelText("Slug"), "a-slug{Enter}");

    expect(onSubmit).toHaveBeenCalled();
  });
});

describe("focus", () => {
  it("moves into the dialog when it opens", () => {
    draw();

    expect(screen.getByLabelText("Slug")).toHaveFocus();
  });

  it("lands on the first focusable thing even when that is a button", () => {
    draw({}, <button type="button">Only button</button>);

    expect(screen.getByRole("button", { name: "Only button" })).toHaveFocus();
  });

  it("returns to whatever opened it", async () => {
    function Page() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>Open</button>
          {open && (
            <Dialog label="Publish" onClose={() => setOpen(false)}>
              <button onClick={() => setOpen(false)}>Cancel</button>
            </Dialog>
          )}
        </>
      );
    }
    render(<Page />);
    const opener = screen.getByRole("button", { name: "Open" });
    await userEvent.click(opener);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(opener).toHaveFocus();
  });

  it("does not chase a trigger that the dialog's own action removed", async () => {
    // A dialog opened from a row it then deletes: the opener is detached by the
    // time focus would go back to it, and `focus()` on a detached node quietly
    // moves focus to the body instead of erroring.
    function Page() {
      const [rowGone, setRowGone] = useState(false);
      const [open, setOpen] = useState(false);
      return (
        <>
          {!rowGone && <button onClick={() => setOpen(true)}>Delete row</button>}
          {open && (
            <Dialog label="Confirm" onClose={() => setOpen(false)}>
              <button
                onClick={() => {
                  setRowGone(true);
                  setOpen(false);
                }}
              >
                Confirm
              </button>
            </Dialog>
          )}
        </>
      );
    }
    render(<Page />);
    await userEvent.click(screen.getByRole("button", { name: "Delete row" }));

    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(screen.queryByRole("button", { name: "Delete row" })).not.toBeInTheDocument();
  });
});

describe("the focus trap", () => {
  it("wraps forward from the last focusable back to the first", async () => {
    draw();
    const slug = screen.getByLabelText("Slug");
    const cancel = screen.getByRole("button", { name: "Cancel" });

    cancel.focus();
    await userEvent.tab();

    expect(slug).toHaveFocus();
  });

  it("wraps backward from the first focusable to the last", async () => {
    draw();
    const slug = screen.getByLabelText("Slug");
    const cancel = screen.getByRole("button", { name: "Cancel" });

    expect(slug).toHaveFocus();
    await userEvent.tab({ shift: true });

    expect(cancel).toHaveFocus();
  });

  it("leaves an ordinary Tab between two fields alone", async () => {
    draw();

    await userEvent.tab();

    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("skips a disabled control, which cannot be tabbed to anyway", async () => {
    draw(
      {},
      <>
        <input aria-label="First" />
        <button type="button" disabled>
          Busy
        </button>
        <button type="button">Last</button>
      </>,
    );

    screen.getByRole("button", { name: "Last" }).focus();
    await userEvent.tab();

    expect(screen.getByLabelText("First")).toHaveFocus();
  });

  it("does nothing on Tab when there is nothing focusable to trap", () => {
    draw({}, <p>Nothing to focus</p>);

    // The point is that this does not throw on an empty focusable list.
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Tab" });

    expect(screen.getByText("Nothing to focus")).toBeInTheDocument();
  });
});

describe("dismissing", () => {
  it("closes on Escape", async () => {
    const onClose = vi.fn();
    draw({ onClose });

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on a click that starts and ends on the backdrop", () => {
    const onClose = vi.fn();
    draw({ onClose });

    fireEvent.mouseDown(screen.getByRole("dialog"));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores a mousedown inside the panel", () => {
    const onClose = vi.fn();
    draw({ onClose });

    fireEvent.mouseDown(screen.getByLabelText("Slug"));

    expect(onClose).not.toHaveBeenCalled();
  });

  it("refuses Escape while a submit is in flight", async () => {
    const onClose = vi.fn();
    draw({ onClose, closable: false });

    await userEvent.keyboard("{Escape}");

    expect(onClose).not.toHaveBeenCalled();
  });

  it("refuses a backdrop click while a submit is in flight", () => {
    const onClose = vi.fn();
    draw({ onClose, closable: false });

    fireEvent.mouseDown(screen.getByRole("dialog"));

    expect(onClose).not.toHaveBeenCalled();
  });

  it("still traps focus while it is unclosable", async () => {
    draw({ closable: false });

    screen.getByRole("button", { name: "Cancel" }).focus();
    await userEvent.tab();

    expect(screen.getByLabelText("Slug")).toHaveFocus();
  });
});

describe("the page behind it", () => {
  it("stops scrolling while the dialog is open", () => {
    const { unmount } = draw();

    expect(document.body.style.overflow).toBe("hidden");

    unmount();
    expect(document.body.style.overflow).toBe("");
  });

  it("gets its own overflow back rather than a blank one", () => {
    document.body.style.overflow = "scroll";
    const { unmount } = draw();
    unmount();

    expect(document.body.style.overflow).toBe("scroll");
    document.body.style.overflow = "";
  });
});

describe("widths", () => {
  it("defaults to the width most of these dialogs use", () => {
    draw();

    expect(screen.getByRole("dialog").firstChild).toHaveClass("max-w-lg");
  });

  it("takes a wider panel for the forms that need one", () => {
    draw({ width: "3xl" });

    expect(screen.getByRole("dialog").firstChild).toHaveClass("max-w-3xl");
  });
});
