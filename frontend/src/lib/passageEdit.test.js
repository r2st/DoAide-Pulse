import { describe, expect, it } from "vitest";
import {
  MAX_SELECTION_CHARS,
  MIN_SELECTION_CHARS,
  OPERATIONS,
  normalizeSelection,
  selectionProblem,
  spliceSelection,
} from "./passageEdit";

const BODY = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here.";

describe("normalizeSelection", () => {
  it("reads the span between the two offsets", () => {
    expect(normalizeSelection(BODY, 0, 21)).toEqual({
      start: 0,
      end: 21,
      text: "First paragraph here.",
    });
  });

  it("drops the trailing newlines a paragraph drag picks up", () => {
    // The regression this guards: the model is asked for a passage and returns
    // one without a blank line after it, so a range that included the blank
    // line welds the next paragraph onto the end of the replacement.
    const selection = normalizeSelection(BODY, 0, 23);
    expect(selection.text).toBe("First paragraph here.");
    expect(selection.end).toBe(21);
  });

  it("drops leading whitespace too", () => {
    const selection = normalizeSelection(BODY, 21, 45);
    expect(selection.text).toBe("Second paragraph here.");
    expect(BODY.slice(selection.start, selection.end)).toBe(selection.text);
  });

  it("copes with a backwards drag", () => {
    expect(normalizeSelection(BODY, 21, 0).text).toBe("First paragraph here.");
  });

  it("is null for a collapsed caret", () => {
    expect(normalizeSelection(BODY, 7, 7)).toBeNull();
  });

  it("is null for a selection of nothing but whitespace", () => {
    expect(normalizeSelection(BODY, 21, 23)).toBeNull();
  });

  it("clamps a range that runs past the end of the body", () => {
    const selection = normalizeSelection("short body text", 0, 999);
    expect(selection.text).toBe("short body text");
  });
});

describe("selectionProblem", () => {
  it("asks for a selection when there is none", () => {
    expect(selectionProblem(null)).toMatch(/Select a passage/);
  });

  it("says how much more is needed for a passage that is too short", () => {
    const selection = { start: 0, end: 3, text: "abc" };
    expect(selectionProblem(selection)).toMatch(
      new RegExp(`${MIN_SELECTION_CHARS} characters`),
    );
  });

  it("points at regeneration for a passage that is really the whole piece", () => {
    const text = "x".repeat(MAX_SELECTION_CHARS + 1);
    expect(selectionProblem({ start: 0, end: text.length, text })).toMatch(
      /regenerate the piece/,
    );
  });

  it("passes an ordinary paragraph", () => {
    expect(selectionProblem(normalizeSelection(BODY, 0, 21))).toBeNull();
  });

  it("accepts a selection of exactly the minimum length", () => {
    const text = "x".repeat(MIN_SELECTION_CHARS);
    expect(selectionProblem({ start: 0, end: text.length, text })).toBeNull();
  });
});

describe("spliceSelection", () => {
  it("puts the replacement where the passage was", () => {
    const selection = normalizeSelection(BODY, 23, 45);
    const result = spliceSelection(BODY, selection, "Shorter second.");

    expect(result.body).toBe(
      "First paragraph here.\n\nShorter second.\n\nThird paragraph here.",
    );
  });

  it("leaves the new selection over the replacement, so edits chain", () => {
    const selection = normalizeSelection(BODY, 23, 45);
    const result = spliceSelection(BODY, selection, "Shorter second.");

    expect(result.selection.text).toBe("Shorter second.");
    expect(result.body.slice(result.selection.start, result.selection.end)).toBe(
      "Shorter second.",
    );
  });

  it("follows the passage when typing above it moved the offsets", () => {
    // The author kept writing while the model was thinking. The stale offsets
    // now point at the wrong text, and trusting them would paste the edited
    // paragraph over the middle of a different one.
    const selection = normalizeSelection(BODY, 23, 45);
    const moved = `A new opening paragraph.\n\n${BODY}`;

    const result = spliceSelection(moved, selection, "Shorter second.");

    expect(result.body).toBe(
      "A new opening paragraph.\n\nFirst paragraph here.\n\nShorter second." +
        "\n\nThird paragraph here.",
    );
    expect(result.body).toContain("First paragraph here.");
  });

  it("refuses when the passage is gone entirely", () => {
    // Nothing to replace: the paragraph the edit was for no longer exists, and
    // splicing at the stale offsets would overwrite whatever took its place.
    const selection = normalizeSelection(BODY, 23, 45);
    expect(spliceSelection("A wholly different draft.", selection, "x")).toBeNull();
  });

  it("does not lose the rest of the body when the passage repeats", () => {
    const body = "Same words twice. Same words twice.";
    const selection = { start: 18, end: 35, text: "Same words twice." };
    const result = spliceSelection(body, selection, "Once only.");

    expect(result.body).toBe("Same words twice. Once only.");
  });
});

describe("the operation list", () => {
  it("names every operation the server accepts", () => {
    // Kept in step by hand with EditOperation in inline_edit.py: a button for
    // an operation the server does not have is a 422 on click.
    expect(OPERATIONS.map((operation) => operation.value).sort()).toEqual([
      "code_example",
      "expand",
      "proofread",
      "retone",
      "rewrite",
      "shorten",
      "simplify",
      "technical",
    ]);
  });

  it("explains each one, since the label alone is a promise", () => {
    for (const operation of OPERATIONS) {
      expect(operation.title.length).toBeGreaterThan(10);
    }
  });
});
