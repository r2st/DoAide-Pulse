# H027 — Computation Correctness (M2, Pass 4)

**Date:** 2026-10-06
**Methodology:** M2 — computation correctness (rounding errors, off-by-one, formula inconsistencies, unit mismatches)

## Findings

### Finding 1: `truncate_at_sentence` off-by-one on first sentence

**File:** `backend/app/services/seo.py:78–84`

**Bug:** The sentence-accumulation loop pre-accounted +1 for a space separator on every sentence, including the first. The first sentence has no preceding space, so a sentence fitting exactly at `limit` characters was rejected (the check saw `limit + 1`) and fell through to the word-boundary ellipsis fallback.

**Example:**
```
text = "Hello world ab. More stuff here and beyond."
truncate_at_sentence(text, 15)
# Before: "Hello world…" (word-boundary fallback with ellipsis)
# After:  "Hello world ab." (the complete first sentence, 15 chars)
```

**Impact:** Meta descriptions, excerpts, and title truncations that ended on a sentence boundary exactly at the character limit were unnecessarily clipped mid-word with an ellipsis. Low frequency but visible in output quality.

**Fix:** Changed the space accounting to add +1 only when `kept` is non-empty (i.e., after the first sentence).

### Finding 2: Inconsistent reading-time formula in git publisher

**File:** `backend/app/services/publishers/git.py:398`

**Bug:** The git publisher computed reading time as `max(1, round(word_count / 238))` (238 wpm), while the canonical `read_minutes_for` in `backend/app/models/content.py:176` uses `max(1, round(word_count / 220))` (220 wpm). The same piece showed different reading times on the analytics dashboard and in its git front matter.

**Example:**
```
word_count = 1000
read_minutes_for(1000)           # 5 min (round(1000/220) = round(4.545))
max(1, round(1000 / 238))       # 4 min (round(1000/238) = round(4.202))
```

**Impact:** A blog post committed to a git repository displayed a different reading time ("4 min read") than the analytics dashboard showed for the same piece ("5 min read"). The disagreement is visible to any user comparing their blog's front matter with Pulse's analytics.

**Fix:** Replaced the hardcoded `238` formula with a call to `read_minutes_for(word_count)`, the canonical function every other reading-time computation in the codebase uses.

## Files Changed

| File | Change |
|------|--------|
| `backend/app/services/seo.py` | Fixed first-sentence space accounting in `truncate_at_sentence` |
| `backend/app/services/publishers/git.py` | Replaced hardcoded 238-wpm formula with `read_minutes_for` import |
| `backend/tests/test_h027_computation.py` | 10 test cases covering both findings |

## Test Results

- 10 new tests: all pass
- 79 existing SEO tests: all pass
- 43 existing git publisher tests: all pass
