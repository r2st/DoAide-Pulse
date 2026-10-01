import { useEffect, useRef } from "react";

/** Panel widths in use. Named so a caller cannot invent a fifth one by hand. */
const WIDTHS = {
  md: "max-w-md",
  lg: "max-w-lg",
  xl: "max-w-xl",
  "3xl": "max-w-3xl",
};

/**
 * What counts as focusable for the purposes of the trap.
 *
 * Deliberately not filtered by visibility: `offsetParent` is null for every
 * element under jsdom, so a visibility check would empty this list in tests
 * while passing in a browser — and everything inside an open dialog is visible
 * anyway, which is the only tree this selector is ever run against.
 */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/**
 * A modal dialog: backdrop, panel, and the keyboard behaviour that makes
 * `aria-modal` true rather than merely claimed.
 *
 *   <Dialog label="Publish" onClose={close} closable={!busy} onSubmit={submit}>
 *     <h2 className="font-display text-2xl text-ink-900">Publish</h2>
 *     …
 *   </Dialog>
 *
 * Pulse had eight of these written out by hand. Each one announced itself as
 * `aria-modal="true"` and none of them behaved like it: Tab walked straight out
 * the back of the dialog into the page it was covering, Escape did nothing, and
 * focus was wherever the last click left it — which for a dialog opened from the
 * keyboard is nowhere useful. A keyboard user could not open one, fill it in and
 * close it without losing their place. The four things below are what that
 * attribute is a promise about:
 *
 *  - focus moves into the dialog when it opens, to the first thing you would
 *    type into;
 *  - Tab and Shift+Tab cycle within it rather than escaping behind the backdrop;
 *  - Escape closes it, the same as the Cancel button;
 *  - focus returns to whatever opened it, so dismissing a dialog puts you back
 *    where you were instead of at the top of the document.
 *
 * A dialog mid-submit is not closable — the request is already in flight and
 * dismissing the form would leave the user with no idea whether it landed. That
 * covers the backdrop click, Escape, and nothing else: the buttons disable
 * themselves.
 *
 * @param {object} props
 * @param {string} props.label Names the dialog for assistive technology. There
 *   is a visible heading in every one of these too, but the accessible name has
 *   to survive a screen reader announcing the dialog before reading its content.
 * @param {() => void} props.onClose
 * @param {boolean} [props.closable] False while a submit is in flight.
 * @param {keyof typeof WIDTHS} [props.width]
 * @param {(event: import("react").FormEvent) => void} [props.onSubmit] Makes the
 *   panel a `<form>`, so Enter in a field submits.
 * @param {import("react").ReactNode} props.children
 */
export default function Dialog({
  label,
  onClose,
  closable = true,
  width = "lg",
  onSubmit,
  children,
}) {
  const panel = useRef(null);

  // Focus in on mount, and back out on unmount. One effect, because the element
  // to return focus to is the one that was active when the dialog opened —
  // reading it in a separate effect would race the focus move below.
  useEffect(() => {
    const opener = document.activeElement;
    focusables(panel.current)[0]?.focus();
    return () => {
      // Only if it is still in the document: a dialog that deleted the row its
      // own trigger lived in has nothing to give focus back to, and calling
      // `focus()` on a detached node silently sends focus to the body.
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);

  // The backdrop scrolls the dialog; the page behind it should not scroll too.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  function onKeyDown(event) {
    if (event.key === "Escape" && closable) {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;

    const items = focusables(panel.current);
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    // `document.activeElement` rather than `event.target`: they differ when the
    // event is retargeted, and it is the focused element the wrap is about.
    const active = document.activeElement;

    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const Panel = onSubmit ? "form" : "div";

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/60 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      // Mousedown rather than click: a click that starts inside the panel and
      // ends on the backdrop — a drag that overshoots while selecting text —
      // should not dismiss the form it was selecting from.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && closable) onClose();
      }}
      onKeyDown={onKeyDown}
    >
      <Panel
        ref={panel}
        onSubmit={onSubmit}
        className={`panel my-auto w-full ${WIDTHS[width]} space-y-4 p-6 shadow-pop`}
      >
        {children}
      </Panel>
    </div>
  );
}

/**
 * The focusable elements inside `root`, in tab order.
 *
 * Document order is tab order here because nothing in these dialogs sets a
 * positive `tabindex` — and nothing should: a hand-numbered tab order inside a
 * form is a bug waiting for the next field to be added in the middle.
 *
 * @param {HTMLElement | null} root
 * @returns {HTMLElement[]}
 */
function focusables(root) {
  if (!root) return [];
  return [...root.querySelectorAll(FOCUSABLE)].filter(
    (element) => !element.hasAttribute("hidden") && element.tabIndex !== -1,
  );
}
