# H025 — Performance

**Methodology:** M8 (performance)  
**Pass:** 4  
**Date:** 2026-10-06  
**Findings:** 2

---

## Finding 1: Cubic scan in `content_engagement._trend()`

**File:** `backend/app/services/content_engagement.py`  
**Lines:** 257–261 (before fix)

**Problem:** The `_trend()` function builds a merged timeline across all of a
piece's publications by iterating every unique timestamp (S stamps), and for
each one scanning every curve's entire points list from the start via a list
comprehension filter:

```python
for hours in stamps:
    for curve in curves:
        due = [p for p in curve.points if round(p.hours, 2) <= hours]
```

This is O(S × C × P) where S = total unique stamps, C = number of curves, and
P = average points per curve. The points are already sorted by hours and the
stamps are sorted too, so re-scanning from index 0 on every iteration is
redundant work. A piece cross-posted to 3 platforms with 200 snapshots each
produces ~600 stamps and re-scans ~600 points per stamp per curve —
approximately 1,080,000 comparisons.

**Fix:** Replace the list-comprehension filter with a per-curve cursor that
advances forward through the sorted points. Each point is visited at most once,
reducing the inner loop from O(P) to amortised O(1) per stamp per curve. Total
work becomes O(S × C + total_P) instead of O(S × C × P).

**Impact:** The endpoint is `GET /api/v1/content/{id}/engagement`, called on
every content detail page. With the default six-hourly polling over a 30-day
window, a 3-platform piece has ~360 snapshots — the fix turns ~130,000
comparisons into ~1,100.

---

## Finding 2: Tie-vulnerable baseline in `digest._last_readings_before()`

**File:** `backend/app/services/digest.py`  
**Lines:** 148–183 (before fix)

**Problem:** The function grouped on `MAX(captured_at)` and then joined back on
`(publication_id, captured_at)` to fetch the full metric row. Two snapshots
captured in the same second — possible when a platform responds faster than the
clock resolution, or when a retry lands in the same wall-clock second — produce
the same `MAX(captured_at)` and the join returns *both* rows. This causes the
dict comprehension to silently keep whichever row was iterated last (arbitrary),
and in the worst case, a different caller walking the result could see duplicate
baselines for the same publication, double-counting the subtracted baseline in
`_gains()`.

The sister function `analytics_service._pre_window_readings()` already solved
this by using `MAX(id)` — which is monotonic and unique — and joining on id
alone.

**Fix:** Replace `MAX(captured_at)` with `MAX(id)` and join on the single
`metric_id` column, matching the analytics service's pattern. This eliminates
the tie and the join now matches exactly one row per publication.

**Impact:** The weekly digest sweep runs `_last_readings_before` once per user,
every week. While the tie condition is uncommon (two polls in the same second),
when it occurs the duplicate row silently corrupts the baseline subtraction. The
fix also simplifies the join from a two-column match to a single-column primary
key lookup, which is marginally faster on the index.

---

## Tests added

`backend/tests/test_h025_performance.py` — 9 test cases:

- `TestTrendPointerAdvance` (6 tests): verifies the optimised `_trend` matches
  the original O(S·C·P) implementation for empty input, single curve, two
  interleaved curves, shared timestamps, offset curves, and a 600-point stress
  case.
- `TestDigestBaselineMaxId` (3 tests): verifies that two snapshots with
  identical `captured_at` return exactly one row (the higher id), that an empty
  id list returns an empty dict, and that no snapshots before the moment returns
  an empty dict.
