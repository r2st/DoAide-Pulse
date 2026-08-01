// Live length figures for the editor's status line.
//
// These deliberately reimplement the server's arithmetic rather than a nicer
// one of their own. `Content.word_count` and `Content.read_minutes` are what
// the SEO checks grade, what the read-time analytics aggregate, and what the
// "6 min read" byline on a published piece says. A counter that drifted from
// them while you typed would be describing a different document than the one
// Herald is about to publish — and the drift would only surface after saving,
// which is the worst moment to learn the number moved.
//
// The server (app/models/content.py):
//     word_count   = len(self.body_markdown.split())
//     read_minutes = max(1, round(self.word_count / 220))

const WORDS_PER_MINUTE = 220;

/**
 * Words in a body of Markdown, counted the way Python's bare `str.split()`
 * does: split on runs of whitespace, ignore any at the ends.
 *
 * Markdown syntax is counted as written — `**bold**` is one word, a fenced
 * code block contributes its tokens. That is what the server counts too, and
 * an editor that quietly discounted code would disagree with the byline on
 * exactly the posts this app exists to write.
 */
export function countWords(text) {
  if (!text) return 0;
  const trimmed = String(text).trim();
  if (!trimmed) return 0;
  return trimmed.split(/\s+/).length;
}

/**
 * Round half to even — what Python's built-in `round()` does, and what
 * `Math.round` does not.
 *
 * They part company on exact halves: `round(2.5)` is 2 in Python and 3 in JS.
 * At 220 words a minute that is reachable — 550 words is exactly 2.5 minutes —
 * so without this the live figure would sit one minute above the saved one for
 * a handful of real documents. Word counts are never negative, so the positive
 * case is the only one that needs handling.
 */
function roundHalfToEven(value) {
  const floor = Math.floor(value);
  const remainder = value - floor;
  if (remainder > 0.5) return floor + 1;
  if (remainder < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/**
 * Reading time in whole minutes, floored at one.
 *
 * The floor is the server's, and it is a judgement rather than a rounding
 * artefact: a two-line changelog entry still takes a moment to read, and "0 min
 * read" reads as a broken counter rather than as a short post.
 */
export function readMinutes(words) {
  return Math.max(1, roundHalfToEven(words / WORDS_PER_MINUTE));
}

/**
 * Everything the status line shows, from the text on screen.
 *
 * One call so the two figures can never be computed from different snapshots
 * of the textarea.
 */
export function editorStats(text) {
  const words = countWords(text);
  return { words, minutes: readMinutes(words) };
}
