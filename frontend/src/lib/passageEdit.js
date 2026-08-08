/**
 * The rules the passage-edit toolbar runs on, kept out of the component.
 *
 * All of this is arithmetic on two strings and a pair of offsets, and every
 * interesting case — the passage that moved while the model was thinking, the
 * selection that is mostly a trailing newline — is a case you would otherwise
 * have to reproduce through a textarea to test at all.
 */

/** Mirrors ``MIN_SELECTION_CHARS`` / ``MAX_SELECTION_CHARS`` in
 *  `backend/app/services/inline_edit.py`. Duplicated rather than fetched: they
 *  bound a *disabled button*, and a toolbar that cannot say why it is inert
 *  until a round trip comes back is worse than one that repeats two numbers.
 *  The server re-checks both, so a drift here costs a 422, not a bad edit. */
export const MIN_SELECTION_CHARS = 12;
export const MAX_SELECTION_CHARS = 12000;

/**
 * The operations, in the order they appear in the toolbar.
 *
 * Ordered by how often they get used rather than alphabetically, and `title`
 * is what the button promises — the author has to be able to tell at a glance
 * whether the model did what was asked, which is also why "make it better" is
 * not on the list.
 */
export const OPERATIONS = [
  { value: "rewrite", label: "Rewrite", title: "Clearer sentences, same length, same points" },
  { value: "shorten", label: "Shorten", title: "Cut to about half, keeping every distinct point" },
  { value: "expand", label: "Expand", title: "Draw out what it already says, roughly double" },
  { value: "simplify", label: "Simplify", title: "Plainer language, shorter sentences" },
  { value: "technical", label: "Technical", title: "The mechanism rather than the summary" },
  { value: "code_example", label: "Add code", title: "Illustrate it with a short runnable example" },
  { value: "proofread", label: "Proofread", title: "Spelling, grammar and typos — nothing else" },
  { value: "retone", label: "Retone", title: "Rewrite in the tone chosen alongside" },
];

/** The tone choices for `retone`. The empty value means "the project's own",
 *  which is the common case — "make this sound like the rest of my writing" —
 *  and the one that sends no tone at all and lets the server decide. */
export const TONES = [
  { value: "", label: "Project voice" },
  { value: "technical", label: "Technical" },
  { value: "casual", label: "Casual" },
  { value: "marketing", label: "Marketing" },
];

/**
 * A textarea's selection, trimmed to the text that is actually being edited.
 *
 * Dragging over a paragraph almost always takes the newline after it as well,
 * and the model is asked for a passage, not a passage plus its blank line — so
 * the replacement comes back without one and the splice welds two paragraphs
 * together. Narrowing the *range* here rather than trimming the string keeps
 * the offsets and the text describing the same span, which is what the splice
 * later relies on.
 *
 * Returns `null` when the selection is empty or all whitespace.
 */
export function normalizeSelection(body, start, end) {
  if (typeof start !== "number" || typeof end !== "number") return null;
  let from = Math.max(0, Math.min(start, end));
  let to = Math.min(body.length, Math.max(start, end));
  while (from < to && /\s/.test(body[from])) from += 1;
  while (to > from && /\s/.test(body[to - 1])) to -= 1;
  if (from >= to) return null;
  return { start: from, end: to, text: body.slice(from, to) };
}

/**
 * Why this selection cannot be sent, or `null` if it can.
 *
 * Phrased as the sentence shown under the toolbar: a disabled button with no
 * explanation reads as broken, and "select more text" is the whole fix.
 */
export function selectionProblem(selection) {
  if (!selection) return "Select a passage in the body to edit it.";
  if (selection.text.length < MIN_SELECTION_CHARS) {
    return `Select at least ${MIN_SELECTION_CHARS} characters — there is not enough here to work on.`;
  }
  if (selection.text.length > MAX_SELECTION_CHARS) {
    return "That passage is too long to edit in place. Select less, or regenerate the piece.";
  }
  return null;
}

/**
 * Put *replacement* where *selection* used to be.
 *
 * The offsets are checked against the body rather than trusted, because they
 * were taken before a network round trip and the author may well have kept
 * typing during it — the model is not fast. If the text is no longer at those
 * offsets but is still somewhere in the body it has simply moved, and the
 * first occurrence is the honest guess; if it is gone entirely, so is the
 * paragraph the edit was for, and splicing anywhere would be vandalism.
 *
 * Returns `{ body, selection }` — the new selection covers the replacement, so
 * operations chain (shorten, then proofread the result) without reselecting —
 * or `null` when the passage can no longer be found.
 */
export function spliceSelection(body, selection, replacement) {
  let { start, end } = selection;
  if (body.slice(start, end) !== selection.text) {
    const moved = body.indexOf(selection.text);
    if (moved < 0) return null;
    start = moved;
    end = moved + selection.text.length;
  }
  return {
    body: body.slice(0, start) + replacement + body.slice(end),
    selection: { start, end: start + replacement.length, text: replacement },
  };
}
