import { describe, expect, it } from "vitest";
import { countWords, editorStats, readMinutes } from "./editorStats";

/** A body of exactly `n` words, to drive the reading-time boundaries. */
function words(n) {
  return Array.from({ length: n }, (_, i) => `w${i}`).join(" ");
}

describe("countWords", () => {
  it("counts nothing in an empty or absent body", () => {
    expect(countWords("")).toBe(0);
    expect(countWords(null)).toBe(0);
    expect(countWords(undefined)).toBe(0);
  });

  it("counts nothing in a body that is only whitespace", () => {
    expect(countWords("   \n\n\t  ")).toBe(0);
  });

  it("ignores whitespace at the ends", () => {
    expect(countWords("  hello world  ")).toBe(2);
  });

  it("treats a run of whitespace as one separator", () => {
    // The naive `split(" ")` returns 5 here, three of them empty. Python's
    // bare `str.split()` — which the server uses — returns 2.
    expect(countWords("hello    world")).toBe(2);
    expect(countWords("hello\n\n\nworld")).toBe(2);
    expect(countWords("hello\t \n world")).toBe(2);
  });

  it("counts Markdown syntax as written, the way the server does", () => {
    // `**bold**` is one token to `str.split()`, so it is one token here.
    expect(countWords("A **bold** claim")).toBe(3);
    expect(countWords("- item one\n- item two")).toBe(6);
  });

  it("counts fenced code, because the server's word_count does", () => {
    // Text ```js const a = 1; ``` More — the fences are tokens too.
    expect(countWords("Text\n\n```js\nconst a = 1;\n```\n\nMore")).toBe(8);
  });
});

describe("readMinutes", () => {
  it("floors at one minute rather than reporting zero", () => {
    // "0 min read" reads as a broken counter, not as a short post.
    expect(readMinutes(0)).toBe(1);
    expect(readMinutes(1)).toBe(1);
    expect(readMinutes(109)).toBe(1);
  });

  it("rounds to the nearest whole minute at 220wpm", () => {
    expect(readMinutes(331)).toBe(2);
    expect(readMinutes(549)).toBe(2);
    expect(readMinutes(551)).toBe(3);
    expect(readMinutes(1000)).toBe(5);
  });

  it("rounds an exact half to even, matching Python's round()", () => {
    // The whole reason this module does not use Math.round. Verified against
    // the server's own `max(1, round(word_count / 220))`:
    //   330 -> 2   550 -> 2   770 -> 4   990 -> 4
    // Math.round would say 2, 3, 4, 5 — disagreeing on two of the four.
    expect(readMinutes(330)).toBe(2);
    expect(readMinutes(550)).toBe(2);
    expect(readMinutes(770)).toBe(4);
    expect(readMinutes(990)).toBe(4);
  });
});

describe("editorStats", () => {
  it("reports both figures from one snapshot of the text", () => {
    expect(editorStats("one two three")).toEqual({ words: 3, minutes: 1 });
  });

  it("agrees with the server across the length boundaries", () => {
    // Each pair is (word count, what app/models/content.py returns).
    const expected = [
      [0, 1],
      [110, 1],
      [330, 2],
      [550, 2],
      [770, 4],
      [990, 4],
      [991, 5],
    ];
    for (const [count, minutes] of expected) {
      expect(editorStats(words(count))).toEqual({ words: count, minutes });
    }
  });

  it("survives an empty body", () => {
    expect(editorStats("")).toEqual({ words: 0, minutes: 1 });
  });
});
